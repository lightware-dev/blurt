#!/usr/bin/env python3
"""Build the NVFP4 (4-bit) snapshot blurtd loads with PARAKEET_DTYPE=nvfp4.

Quantizes the encoder's 217 Linear layers to NVFP4 — E2M1 values in blocks of 16,
each block scaled by an E4M3 fp8 factor — and writes a self-contained snapshot
directory (see server/nvfp4.py for the format). Weights drop from 1.275 GB to
0.509 GB with no WER cost this repo's eval corpus can detect.

Unlike the bf16/fp16 checkpoints, which are a pure cast, this needs *calibration
audio*: modelopt picks activation scales from ranges it observes on real speech.
Calibrate on audio the model has not been scored against — reusing evaluation
clips tunes the quantizer to its own test set. Build a corpus with
scripts/make_eval_corpus.py and pass a slice of it here.

    # one-off: a corpus to calibrate from
    python scripts/make_eval_corpus.py --out /tmp/calib --parquet test-clean.parquet \
        --limit 32

    python scripts/build_nvfp4_snapshot.py --calib /tmp/calib --out ~/.cache/blurt/parakeet-tdt-0.6b-v3-nvfp4

Needs a bf16-capable CUDA GPU (calibration runs the real model) and
nvidia-modelopt. Takes about 35 s. Verify the result with --verify, which
reloads the snapshot in isolation and checks it reproduces the transcripts the
quantized model produced in memory.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server.asr as asr_mod  # noqa: E402  (applies the NumPy 2 sctypes shim)
from server import nvfp4  # noqa: E402
from server.asr import SAMPLE_RATE, ParakeetASR  # noqa: E402

# E2M1 weights in blocks of 16, each block carrying an E4M3 fp8 scale.
NVFP4_WEIGHT_CFG = {"num_bits": (2, 1),
                    "block_sizes": {-1: 16, "type": "dynamic", "scale_bits": (4, 3)}}


def load_wav(path: str) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    return np.ascontiguousarray(audio, dtype=np.float32)


def source_nemo() -> str:
    """The bf16 .nemo to quantize from, downloading it if needed."""
    ckpt = ParakeetASR(precision="bf16").ckpt_path()
    if not os.path.exists(ckpt):
        ckpt = ParakeetASR._download_ckpt("bf16")
    if not ckpt or not os.path.exists(ckpt):
        raise RuntimeError("no bf16 .nemo available to quantize from")
    return ckpt


def extract_config(nemo_path: str, dest: Path):
    """Copy config + tokenizer artifacts out of the .nemo, leaving the weights."""
    with tarfile.open(nemo_path) as tf:
        members = [m for m in tf.getmembers()
                   if m.isfile() and not m.name.endswith("model_weights.ckpt")]
        tf.extractall(dest, members=members)
    from omegaconf import OmegaConf, open_dict

    cfg = OmegaConf.load(dest / nvfp4.CONFIG)
    with open_dict(cfg):
        # `nemo:<file>` resolves inside the tarball; the files now sit beside the
        # config, so reference them by plain name.
        for key in list(cfg.get("tokenizer", {})):
            val = cfg.tokenizer[key]
            if isinstance(val, str) and val.startswith("nemo:"):
                cfg.tokenizer[key] = val[len("nemo:"):]
        # Training/validation dataset blocks reference paths that do not exist here
        # and would make the config fail to instantiate at load time.
        for ds in ("train_ds", "validation_ds", "test_ds"):
            if ds in cfg:
                cfg[ds] = None
    OmegaConf.save(cfg, dest / nvfp4.CONFIG)


def build_quant_config(model, min_numel: int = 1 << 16):
    """Per-layer modelopt rules covering exactly the encoder Linears.

    Generated from the model rather than by wildcard so the quantized set is
    explicit and inspectable: every layer that gets four bits is named in the
    recipe. Convolutions, the LSTM decoder and the joint network are left alone —
    they are 14% of the weights, and the joint's vocab projection runs inside the
    TDT decode loop where the extra unpack per step would cost latency.
    """
    import torch

    rules = [{"quantizer_name": "*", "enable": False}]   # opt in, never out
    targets = []
    for name, mod in model.named_modules():
        if not isinstance(mod, torch.nn.Linear) or not name.startswith("encoder"):
            continue
        if mod.weight.numel() < min_numel:
            continue
        targets.append((name, tuple(mod.weight.shape)))
        rules.append({"quantizer_name": f"{name}.weight_quantizer",
                      "cfg": dict(NVFP4_WEIGHT_CFG)})
    return {"quant_cfg": rules, "algorithm": "max"}, targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calib", required=True,
                    help="corpus dir holding manifest.json (scripts/make_eval_corpus.py)")
    ap.add_argument("--out", default=os.path.expanduser(
        "~/.cache/blurt/parakeet-tdt-0.6b-v3-nvfp4"))
    ap.add_argument("--calib-clips", type=int, default=32)
    ap.add_argument("--verify-clips", type=int, default=20,
                    help="clips whose transcripts are recorded for --verify")
    ap.add_argument("--verify", action="store_true",
                    help="reload the finished snapshot and check it matches")
    args = ap.parse_args()

    import torch
    import modelopt.torch.opt as mto
    import modelopt.torch.quantization as mtq
    from safetensors.torch import save_file

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    items = json.loads((Path(args.calib) / "manifest.json").read_text())
    calib = [load_wav(i["path"]) for i in items[:args.calib_clips]]
    verify_items = items[:args.verify_clips]
    if not calib:
        raise SystemExit(f"no clips in {args.calib}/manifest.json")
    print(f"[nvfp4] calibrating on {len(calib)} clips from {args.calib}", flush=True)

    t0 = time.time()
    extract_config(source_nemo(), out)

    asr = ParakeetASR(precision="bf16")
    model = asr.load()
    cfg, targets = build_quant_config(model)
    print(f"[nvfp4] quantizing {len(targets)} encoder Linears "
          f"({sum(np.prod(s) for _, s in targets)/1e6:.0f}M params)", flush=True)

    def forward_loop(_m):
        for audio in calib:
            asr.transcribe(audio)

    # Calibrate through transcribe(), the path production uses, so the observed
    # activation ranges are the real ones. It also avoids NeMo's quirk that direct
    # forward calls and transcribe() corrupt each other (see server/asr.py).
    # inference_mode is disabled for the pass because modelopt records amax into
    # buffers, and inference tensors cannot be stored in them.
    with torch.inference_mode(False):
        mtq.quantize(model, cfg, forward_loop=forward_loop)

    # Without compress the weights stay bf16 and merely *behave* as 4-bit: the
    # quality is real, the memory saving is not.
    mtq.compress(model)

    reference = [asr.transcribe(load_wav(i["path"])) for i in verify_items]

    nvfp4.write_recipe(mto.modelopt_state(model), str(out / nvfp4.RECIPE))
    state = model.state_dict()
    nontensor = [k for k, v in state.items() if not torch.is_tensor(v)]
    if nontensor:
        raise RuntimeError(f"state_dict holds non-tensor entries safetensors cannot "
                           f"carry: {nontensor[:5]}")
    save_file({k: v.detach().cpu().contiguous() for k, v in state.items()},
              str(out / nvfp4.WEIGHTS))
    # Drop the reference before --verify measures anything: state_dict holds the
    # live CUDA tensors, and leaving it in scope inflates the peak the reload
    # reports (1.3 GB rather than the true 0.78 GB) by keeping this model resident
    # alongside the one being loaded.
    del state

    sizes = {f.name: f.stat().st_size for f in out.iterdir() if f.is_file()}
    (out / nvfp4.MANIFEST).write_text(json.dumps({
        "model_id": asr_mod.MODEL_ID,
        "format": "nvfp4-w4a16",
        "source_nemo": os.path.basename(source_nemo()),
        "layers_quantized": len(targets),
        "calib_clips": len(calib),
        "calib_corpus": str(args.calib),
        "built_s": round(time.time() - t0, 1),
        "torch": torch.__version__,
        "verify_ids": [i["id"] for i in verify_items],
        "verify_transcripts": reference,
    }, indent=2))

    total = sum(sizes.values())
    print(f"\n[nvfp4] snapshot -> {out}")
    for name, size in sorted(sizes.items(), key=lambda kv: -kv[1]):
        print(f"    {name:24s} {size/1e6:8.1f} MB")
    print(f"    {'TOTAL':24s} {total/1e6:8.1f} MB   "
          f"(source .nemo {os.path.getsize(source_nemo())/1e6:.0f} MB)")
    print(f"[nvfp4] built in {time.time()-t0:.0f}s")

    if args.verify:
        del asr, model
        import gc
        gc.collect(); torch.cuda.empty_cache()
        verify(out, verify_items, reference)


def verify(out: Path, verify_items: list, reference: list):
    """Reload the snapshot and require the exact transcripts it was built with.

    Fluent-looking output is not evidence: a mis-bound scale buffer still yields
    plausible English. Only an exact match shows the packed weights and their
    scales were restored as the quantized model had them.
    """
    import torch

    print("\n[nvfp4] verifying: reloading snapshot from disk ...", flush=True)
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()
    model = nvfp4.load_snapshot(str(out), device="cuda")
    peak = torch.cuda.max_memory_allocated() / 1e9
    print(f"[nvfp4] loaded in {time.time()-t0:.1f}s, peak CUDA {peak:.3f} GB", flush=True)

    got = []
    for item in verify_items:
        with torch.inference_mode():
            got.append(asr_mod._extract_text(
                model.transcribe([load_wav(item["path"])], batch_size=1, verbose=False)))
    same = sum(a == b for a, b in zip(reference, got))
    for item, want, have in zip(verify_items, reference, got):
        if want != have:
            print(f"    MISMATCH {item['id']}\n      built: {want}\n      load : {have}")
    print(f"[nvfp4] transcripts identical to the in-memory model: {same}/{len(reference)}")
    if same != len(reference):
        raise SystemExit("snapshot does not reproduce the model it was built from")


if __name__ == "__main__":
    main()
