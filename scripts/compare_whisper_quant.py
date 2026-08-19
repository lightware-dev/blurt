#!/usr/bin/env python3
"""Compare Whisper decode quality, speed and VRAM across 4-bit weight formats.

The question this answers is the Whisper half of what scripts/compare_precision.py
answers for Parakeet: *is there a 4-bit option worth shipping, and what does it
cost?* server/whisper.py currently says there is none — this is the measurement
that either backs that up or replaces it.

Four kinds of evidence per variant, because a 4-bit model can fail in ways a WER
table alone will not show:

  1. WER / CER against ground truth, per corpus slice, with a paired bootstrap CI
     on (variant - bf16). The difference is what matters, not either absolute.
  2. Failure counts a WER average hides: empty transcripts, and runaway output
     (Whisper's documented habit of filling its 30 s window with invented text,
     which 4-bit weights can make worse). A hallucinating clip contributes a
     handful of word errors but ruins the dictation it lands in.
  3. VRAM: resident after load, sampled mean and peak across the decode run, at
     both the torch-allocator level (this process only) and the driver level
     (what the card actually gives up).
  4. Latency: per-clip decode wall time and RTF over a duration-spread timing set.

Variants (--variants, default all):

  bf16        the shipping default: WhisperForConditionalGeneration in bfloat16
  fp16        the sm_75 fallback, for reference
  nvfp4       modelopt W4A16 NVFP4 — E2M1 in blocks of 16, fp8 (E4M3) block
              scale, max-calibrated. The same recipe server/nvfp4.py ships for
              Parakeet, so a positive result reuses that snapshot machinery.
  nvfp4-awq   the same NVFP4 weights, with AWQ-lite searching a per-channel
              scale before rounding instead of taking the observed max
  int4-awq    modelopt INT4 group-128 + AWQ-lite. Not Blackwell-specific, so it
              is the option that would also help the Turing/Ampere cards.
  nf4         bitsandbytes NF4 (double-quantized, bf16 compute) — the de-facto
              "load_in_4bit" path. Needs `pip install bitsandbytes`; it is not in
              requirements.txt and would be a new dependency to take on.

The modelopt variants are quantized *in this process* from the bf16 weights, then
`mtq.compress`ed so the packed 4-bit tensors are what stays resident. That makes
the steady-state and peak-decode VRAM figures real, but not the load-time ones:
building the quantized model needs the bf16 model on the card first. A shipped
4-bit Whisper would be a pre-quantized snapshot (server/nvfp4.py), which loads
packed weights straight onto the device — so `weights_gb` here is the number that
would carry over, and `build_peak_gb` is a one-off offline cost, reported so the
distinction stays visible.

Usage:
    python scripts/compare_whisper_quant.py --corpus /scratch/corpus \
        --calib /scratch/calib --out whisper-quant.json
    python scripts/compare_whisper_quant.py --corpus ... --calib ... \
        --variants bf16 nvfp4          # a subset
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import gc
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Scoring, normalisation and the paired bootstrap are identical to the Parakeet
# study — reused rather than re-derived so the two reports are comparable.
# Imported by its bare name off this directory, not as `scripts.compare_precision`:
# an installed dependency already owns the top-level name `scripts`, and a real
# package beats a namespace one whatever the path order.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from compare_precision import (  # noqa: E402
    corpus_wer, load_wav, normalise, paired_bootstrap, score)
from server.asr import SAMPLE_RATE  # noqa: E402
from server.whisper import WhisperASR  # noqa: E402

# Everything four-bit here is weight-only (W4A16): activations stay bf16. 4-bit
# activations are a separate trade (NVFP4_DEFAULT_CFG) that buys throughput at
# batch sizes a dictation server never reaches, and costs accuracy it cannot afford.
MODELOPT_VARIANTS = {
    "nvfp4": ("W4A16_NVFP4_CFG", "max"),
    "nvfp4-awq": ("W4A16_NVFP4_CFG", {"method": "awq_lite", "alpha_step": 0.1}),
    "int4-awq": ("INT4_AWQ_CFG", None),   # its stock algorithm is already awq_lite
}
DTYPE_VARIANTS = ("bf16", "fp16")
BNB_VARIANTS = ("nf4",)
ALL_VARIANTS = list(DTYPE_VARIANTS) + list(MODELOPT_VARIANTS) + list(BNB_VARIANTS)

# The encoder's two front convolutions see raw mel energies, and are 1% of the
# weights — the same reasoning that left Parakeet's convs in bf16. modelopt's
# stock configs already skip proj_out (tied to the token embedding, so packing it
# would corrupt the embedding) and nn.Embedding.
CONV_EXCLUSIONS = ("*conv1*", "*conv2*")

# Output longer than this many characters per second of audio is not a
# transcript. Speech runs ~15 chars/s; Whisper's failure mode is to keep
# generating until the token cap, which lands far above any real rate.
RUNAWAY_CHARS_PER_S = 40.0
# ...but a short clip can legitimately be dense, so require some absolute size too.
RUNAWAY_MIN_CHARS = 200


# ---------------------------------------------------------------- VRAM sampling

def _smi(query: str) -> list[str]:
    out = subprocess.run(["nvidia-smi", f"--query-{query}", "--format=csv,noheader,nounits"],
                         capture_output=True, text=True, check=True)
    return [line.strip() for line in out.stdout.splitlines() if line.strip()]


# Set by proc_vram_gb: "pid" when the driver reported our own process, "total-baseline"
# when it did not and whole-card usage minus the pre-run baseline stood in.
VRAM_SOURCE = "unknown"


def proc_vram_gb(baseline_gb: float | None = None) -> float:
    """VRAM this process holds on the card, in GB, driver's view.

    The torch allocator only knows about tensors: the CUDA context, cuBLAS/cuDNN
    workspaces and the kernels themselves are another ~0.5 GB that a `nvidia-smi`
    reading includes and `memory_allocated` does not. That total is what decides
    whether the model fits, so it is worth the subprocess.

    nvidia-smi inside a container reports PIDs in the container's namespace, so
    our own PID is matchable; when it is not (some driver/toolkit combinations
    report nothing per-process), fall back to whole-card usage minus a baseline
    captured before the model loaded — correct as long as nothing else on the box
    changes its allocation mid-run, which the report flags.
    """
    global VRAM_SOURCE
    mine = str(os.getpid())
    try:
        for line in _smi("compute-apps=pid,used_gpu_memory"):
            pid, used = [p.strip() for p in line.split(",")]
            if pid == mine:
                VRAM_SOURCE = "pid"
                return float(used) / 1024
    except Exception:
        pass
    VRAM_SOURCE = "total-baseline"
    if baseline_gb is None:
        return float("nan")
    try:
        return max(0.0, float(_smi("gpu=memory.used")[0]) / 1024 - baseline_gb)
    except Exception:
        return float("nan")


class VramSampler:
    """Samples allocator and driver VRAM on a thread while decoding runs.

    Peak alone answers "will it fit"; the mean answers "what does it hold while
    idle-ish between segments", which is what a box running other models cares
    about. The allocator is sampled often because it is free; nvidia-smi is a
    subprocess, so it goes slower.
    """

    def __init__(self, torch, baseline_gb, alloc_hz=40, smi_hz=2):
        self._torch = torch
        self._baseline = baseline_gb
        self._alloc_dt = 1.0 / alloc_hz
        self._smi_every = max(1, int(alloc_hz / smi_hz))
        self.alloc, self.reserved, self.proc = [], [], []
        self._stop = threading.Event()
        self._thread = None

    def _run(self):
        n = 0
        while not self._stop.is_set():
            self.alloc.append(self._torch.cuda.memory_allocated() / 1e9)
            self.reserved.append(self._torch.cuda.memory_reserved() / 1e9)
            if n % self._smi_every == 0:
                got = proc_vram_gb(self._baseline)
                if got == got:  # not NaN
                    self.proc.append(got)
            n += 1
            self._stop.wait(self._alloc_dt)

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=5)

    def summary(self) -> dict:
        def agg(xs):
            return {"mean": float(np.mean(xs)), "max": float(np.max(xs)),
                    "min": float(np.min(xs)), "n": len(xs)} if xs else None
        return {"alloc_gb": agg(self.alloc), "reserved_gb": agg(self.reserved),
                "proc_gb": agg(self.proc)}


# ------------------------------------------------------------- model building

@contextlib.contextmanager
def _calibration_mode(torch):
    """Let the production decode path run as a modelopt calibration pass.

    WhisperASR.transcribe() decodes under `torch.inference_mode()`, and modelopt
    records activation ranges into module buffers — which cannot hold inference
    tensors. Rather than reimplement the decode with the right guard (and risk
    calibrating on a path production never takes), swap the guard itself for the
    duration: transcribe() looks `inference_mode` up on the module at call time.
    """
    saved = torch.inference_mode
    # Entered with the *original* guard, before the swap: modelopt's own
    # bookkeeping runs outside the forward loop, and it too needs writable buffers.
    with saved(False):
        torch.inference_mode = torch.no_grad
        try:
            yield
        finally:
            torch.inference_mode = saved


def quant_config(name: str):
    """The modelopt config for a variant, with the convolutions opted out."""
    import modelopt.torch.quantization as mtq

    cfg_name, algorithm = MODELOPT_VARIANTS[name]
    cfg = copy.deepcopy(getattr(mtq, cfg_name))
    if algorithm is not None:
        cfg["algorithm"] = algorithm
    cfg["quant_cfg"] = list(cfg["quant_cfg"]) + [
        {"quantizer_name": pat, "enable": False} for pat in CONV_EXCLUSIONS]
    return cfg


def quantized_layers(model) -> list[tuple[str, int]]:
    """(name, param count) for every module modelopt actually put in 4 bits."""
    out = []
    for name, mod in model.named_modules():
        wq = getattr(mod, "weight_quantizer", None)
        if wq is not None and getattr(wq, "is_enabled", False) and hasattr(mod, "weight"):
            out.append((name, int(mod.weight.numel())))
    return out


def state_bytes(model) -> int:
    """Bytes the weights would occupy on disk / on load, packed as they are now."""
    import torch

    return sum(v.numel() * v.element_size()
               for v in model.state_dict().values() if torch.is_tensor(v))


class _BnbWhisper(WhisperASR):
    """WhisperASR loaded through bitsandbytes' 4-bit path.

    Only load() differs: transcribe() runs the identical code, so the comparison
    is of weight formats and not of two decode implementations.
    """

    def load(self):
        if self._model is not None:
            return self._model
        import torch
        from transformers import (BitsAndBytesConfig, WhisperForConditionalGeneration,
                                  WhisperProcessor)

        torch.set_num_threads(int(os.getenv("OMP_NUM_THREADS", "2")))
        t0 = time.time()
        qcfg = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            # Quantizes the fp32 block scales too: another ~0.4 bits/weight saved,
            # which is the configuration anyone shipping this would pick.
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=torch.bfloat16,
            # Tied to embed_tokens; 4-bit packing it would take the embedding with it.
            llm_int8_skip_modules=["proj_out"],
        )
        print("[asr] loading whisper (bnb nf4) ...", flush=True)
        model = WhisperForConditionalGeneration.from_pretrained(
            self.model_name, quantization_config=qcfg, dtype=torch.bfloat16,
            device_map={"": 0})
        model.eval()
        if getattr(model.generation_config, "forced_decoder_ids", None) is not None:
            model.generation_config.forced_decoder_ids = None
        self.multilingual = bool(
            getattr(model.generation_config, "is_multilingual", self.multilingual))
        self._processor = WhisperProcessor.from_pretrained(self.model_name)
        # Features are cast to this before the encoder; bf16 is the compute dtype
        # the 4-bit weights dequantize into.
        self.dtype = torch.bfloat16
        self._model = model
        self._torch = torch
        print(f"[asr] ready in {time.time()-t0:.1f}s (nf4/bf16 compute)", flush=True)
        return model


def build(variant: str, calib: list, torch) -> tuple[WhisperASR, dict]:
    """Return a loaded engine for a variant plus what it cost to build.

    Load timing is measured here because it differs by an order of magnitude
    across the options and is part of what a 4-bit path is chosen (or not) for.
    """
    torch.cuda.empty_cache()
    torch.cuda.reset_peak_memory_stats()
    t0 = time.time()

    if variant in DTYPE_VARIANTS:
        asr = WhisperASR(precision=variant)
        asr.load()
        meta = {"format": variant, "quantized_layers": 0, "quantized_params": 0}
    elif variant in BNB_VARIANTS:
        asr = _BnbWhisper()
        asr.load()
        meta = {"format": "bnb-nf4-double", "quantized_layers": None,
                "quantized_params": None}
    else:
        import modelopt.torch.quantization as mtq

        asr = WhisperASR(precision="bf16")
        model = asr.load()
        cfg = quant_config(variant)
        print(f"[cmp] {variant}: calibrating on {len(calib)} clips", flush=True)

        def forward_loop(_m):
            for audio in calib:
                asr.transcribe(audio)

        with _calibration_mode(torch):
            mtq.quantize(model, cfg, forward_loop=forward_loop)
        layers = quantized_layers(model)
        # Without compress the weights stay bf16 and merely *behave* as 4-bit:
        # the quality is real, the memory saving is not.
        mtq.compress(model)
        meta = {"format": variant,
                "quantized_layers": len(layers),
                "quantized_params": sum(n for _, n in layers),
                "layer_names": [n for n, _ in layers[:8]]}

    build_peak = torch.cuda.max_memory_allocated() / 1e9
    gc.collect()
    torch.cuda.empty_cache()
    torch.cuda.synchronize()
    meta.update({
        "build_s": time.time() - t0,
        "build_peak_gb": build_peak,
        "weights_gb": torch.cuda.memory_allocated() / 1e9,
        "state_dict_gb": state_bytes(asr._model) / 1e9,
    })
    return asr, meta


# ------------------------------------------------------------------- the run

def run_variant(variant: str, items: list, audio: dict, calib: list, timing_ids: list,
                timing_repeats: int, release_cache: bool, baseline_gb: float) -> dict:
    """Build one variant, decode the corpus, measure it, and free it again."""
    import torch

    asr, meta = build(variant, calib, torch)
    load_proc = proc_vram_gb(baseline_gb)

    # The first decode pays for lazy kernel/JIT setup, and for the 4-bit variants
    # for the first unpack of every layer. It would otherwise land entirely on
    # whichever clip happens to be first.
    asr.transcribe(audio[items[0]["id"]])
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()

    results = {}
    with VramSampler(torch, baseline_gb) as sampler:
        t_start = time.time()
        for n, item in enumerate(items, 1):
            wav = audio[item["id"]]
            torch.cuda.synchronize()
            t0 = time.time()
            text = asr.transcribe(wav)
            torch.cuda.synchronize()
            dt = time.time() - t0
            results[item["id"]] = {"text": text, "seconds": item["seconds"],
                                   "decode_s": dt, "rtf": dt / max(item["seconds"], 1e-6)}
            if release_cache:
                asr.release_cache()
            if n % 50 == 0:
                print(f"[cmp] {variant}: {n}/{len(items)} decoded", flush=True)
        wall_s = time.time() - t_start
    vram = sampler.summary()
    peak_alloc = torch.cuda.max_memory_allocated() / 1e9
    peak_reserved = torch.cuda.max_memory_reserved() / 1e9

    # Timing on a duration spread, repeated, so the latency figures are medians of
    # a warm model rather than one sample apiece.
    timing = {}
    for uid in timing_ids:
        wav = audio[uid]
        dur = len(wav) / SAMPLE_RATE
        samples = []
        for _ in range(timing_repeats):
            torch.cuda.synchronize()
            t0 = time.time()
            asr.transcribe(wav)
            torch.cuda.synchronize()
            samples.append(time.time() - t0)
        timing[uid] = {"seconds": dur, "latency_s": float(np.median(samples)),
                       "rtf": float(np.median(samples)) / dur}

    decode_s = [r["decode_s"] for r in results.values()]
    rtfs = [r["rtf"] for r in results.values()]
    out = {
        "variant": variant,
        **meta,
        "load_proc_gb": load_proc,
        "peak_alloc_gb": peak_alloc,
        "peak_reserved_gb": peak_reserved,
        "vram_sampled": vram,
        "wall_s": wall_s,
        "audio_s": sum(i["seconds"] for i in items),
        "latency_s": {"median": float(np.median(decode_s)),
                      "mean": float(np.mean(decode_s)),
                      "p90": float(np.percentile(decode_s, 90)),
                      "max": float(np.max(decode_s))},
        "rtf": {"median": float(np.median(rtfs)), "mean": float(np.mean(rtfs))},
        "timing": timing,
        "timing_rtf_median": float(np.median([t["rtf"] for t in timing.values()])),
        "results": results,
    }

    del asr
    gc.collect()
    torch.cuda.empty_cache()
    return out


def failures(items: list, run: dict) -> dict:
    """Per-clip breakdowns a WER average hides."""
    empty, runaway, worst = [], [], []
    for item in items:
        r = run["results"][item["id"]]
        text = r["text"].strip()
        if not text:
            empty.append(item["id"])
        elif (len(text) > RUNAWAY_MIN_CHARS
              and len(text) / max(item["seconds"], 1e-6) > RUNAWAY_CHARS_PER_S):
            runaway.append({"id": item["id"], "seconds": item["seconds"],
                            "chars": len(text), "text": text[:200]})
        s = score(item["text"], text)
        if s["ref_words"] and s["word_errors"] / s["ref_words"] > 0.5:
            worst.append({"id": item["id"], "kind": item["kind"],
                          "wer": s["word_errors"] / s["ref_words"],
                          "ref": item["text"][:120], "hyp": text[:200]})
    worst.sort(key=lambda w: -w["wer"])
    return {"empty": empty, "runaway": runaway, "clips_over_50pc_wer": worst[:15],
            "n_empty": len(empty), "n_runaway": len(runaway),
            "n_broken_clips": len(worst)}


def _load_corpora(args):
    """Eval items + decoded audio + calibration audio, with the overlap check."""
    items = json.loads((Path(args.corpus) / "manifest.json").read_text())
    if args.limit:
        items = items[:args.limit]
    audio = {i["id"]: load_wav(i["path"]) for i in items}

    calib_items = json.loads((Path(args.calib) / "manifest.json").read_text())
    overlap = {i["id"] for i in calib_items} & {i["id"] for i in items}
    if overlap:
        raise SystemExit(
            f"calibration corpus shares {len(overlap)} clips with the eval corpus "
            f"(first: {sorted(overlap)[:3]}). Calibrating on the test set tunes the "
            "quantizer to its own exam.")
    calib = [load_wav(i["path"]) for i in calib_items[:args.calib_clips]]

    by_dur = sorted(items, key=lambda i: i["seconds"])
    timing_ids = [by_dur[int(x)]["id"] for x in
                  np.linspace(0, len(by_dur) - 1, min(args.timing_clips, len(by_dur)))]
    return items, audio, calib, timing_ids


def worker_main(args):
    """Measure one variant and write its run dict to --worker-out.

    Each variant gets its own process because VRAM is the measurement: modelopt's
    calibration leaves ~0.5 GB of live tensors behind that no `del` or
    `empty_cache` in this process reclaims, and in a shared process that lands on
    whichever variant runs *next* — which is exactly how the first pass came to
    report bitsandbytes NF4 at 1.13 GB when it is 0.53 GB on its own. A fresh
    process also means a fresh CUDA context, so the driver-level figure is this
    model's and nothing else's.
    """
    items, audio, calib, timing_ids = _load_corpora(args)
    baseline_gb = float(_smi("gpu=memory.used")[0]) / 1024
    print(f"[cmp] {args.variant_worker}: card holds {baseline_gb:.2f} GB before load",
          flush=True)
    try:
        run = run_variant(args.variant_worker, items, audio, calib, timing_ids,
                          args.timing_repeats, args.release_cache, baseline_gb)
        run["baseline_card_gb"] = baseline_gb
        run["driver_vram_source"] = VRAM_SOURCE
        import torch

        run["gpu"] = torch.cuda.get_device_name(0)
        run["torch"] = torch.__version__
    except Exception as exc:          # a variant that cannot build is a result too
        import traceback
        traceback.print_exc()
        run = {"variant": args.variant_worker, "error": f"{type(exc).__name__}: {exc}"}
    Path(args.worker_out).write_text(json.dumps(run))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="dir holding manifest.json")
    ap.add_argument("--calib", required=True,
                    help="calibration corpus dir; must not overlap --corpus")
    ap.add_argument("--out", default="whisper-quant-results.json")
    ap.add_argument("--variants", nargs="+", default=ALL_VARIANTS)
    ap.add_argument("--baseline", default="bf16",
                    help="variant every other one is compared against")
    ap.add_argument("--calib-clips", type=int, default=32)
    ap.add_argument("--timing-clips", type=int, default=12)
    ap.add_argument("--timing-repeats", type=int, default=3)
    ap.add_argument("--release-cache", action="store_true",
                    help="empty the CUDA cache between clips, as the server does")
    ap.add_argument("--limit", type=int, default=0, help="cap clips (debugging)")
    ap.add_argument("--variant-worker", help=argparse.SUPPRESS)   # see worker_main
    ap.add_argument("--worker-out", help=argparse.SUPPRESS)
    args = ap.parse_args()

    if args.variant_worker:
        return worker_main(args)

    import tempfile

    items = json.loads((Path(args.corpus) / "manifest.json").read_text())
    if args.limit:
        items = items[:args.limit]

    print(f"[cmp] eval corpus: {len(items)} clips, "
          f"{sum(i['seconds'] for i in items)/60:.1f} min", flush=True)

    runs = {}
    with tempfile.TemporaryDirectory() as tmp:
        for variant in args.variants:
            print(f"\n[cmp] ===== {variant} =====", flush=True)
            out = Path(tmp) / f"{variant}.json"
            cmd = [sys.executable, "-u", __file__, "--variant-worker", variant,
                   "--worker-out", str(out), "--corpus", args.corpus,
                   "--calib", args.calib, "--calib-clips", str(args.calib_clips),
                   "--timing-clips", str(args.timing_clips),
                   "--timing-repeats", str(args.timing_repeats),
                   "--limit", str(args.limit)]
            if args.release_cache:
                cmd.append("--release-cache")
            rc = subprocess.run(cmd).returncode
            if rc != 0 or not out.exists():
                runs[variant] = {"variant": variant,
                                 "error": f"worker exited {rc} without a result"}
                continue
            runs[variant] = json.loads(out.read_text())

    ok = {k: v for k, v in runs.items() if "error" not in v}
    if not ok:
        raise SystemExit("[cmp] every variant failed; nothing to compare")
    report = {
        "corpus": args.corpus, "calib": args.calib, "n_clips": len(items),
        "audio_minutes": sum(i["seconds"] for i in items) / 60,
        "isolation": "one subprocess per variant",
        "runs": runs, "slices": {}, "failures": {}, "comparisons": {},
    }
    kinds = sorted({i["kind"] for i in items})
    slices = ([("all", items)]
              + [(k, [i for i in items if i["kind"] == k]) for k in kinds]
              + [("real+degraded", [i for i in items if i["kind"] != "synthetic"])])
    for label, subset in slices:
        if subset:
            report["slices"][label] = {p: corpus_wer(subset, ok[p]) for p in ok}
    for variant in ok:
        report["failures"][variant] = failures(items, ok[variant])

    base = args.baseline
    if base in ok:
        for variant in ok:
            if variant == base:
                continue
            agree = sum(normalise(ok[variant]["results"][i["id"]]["text"])
                        == normalise(ok[base]["results"][i["id"]]["text"]) for i in items)
            report["comparisons"][variant] = {
                "vs": base,
                "wer_diff": paired_bootstrap(items, ok[variant], ok[base]),
                "wer_diff_real_only": paired_bootstrap(
                    [i for i in items if i["kind"] != "synthetic"], ok[variant], ok[base]),
                "identical_after_normalisation": agree,
                "of": len(items),
            }

    Path(args.out).write_text(json.dumps(report, indent=2))
    print_report(report)
    print(f"\n[cmp] full results -> {args.out}")


def print_report(report: dict):
    ok = [k for k, v in report["runs"].items() if "error" not in v]
    runs = report["runs"]

    print("\n=== WER / CER by slice ===")
    print(f"{'slice':20s} {'n':>4s}  " + "  ".join(f"{p:>17s}" for p in ok))
    for label, per in report["slices"].items():
        n = per[ok[0]]["n"]
        cells = "  ".join(f"{per[p]['wer']*100:7.2f}% /{per[p]['cer']*100:6.2f}%" for p in ok)
        print(f"{label:20s} {n:4d}  {cells}")

    print("\n=== VRAM (GB) ===")
    print(f"{'variant':12s} {'weights':>8s} {'load(drv)':>10s} {'avg':>7s} {'peak':>7s} "
          f"{'peak(rsv)':>10s} {'state_dict':>11s} {'build_peak':>11s}")
    for p in ok:
        r = runs[p]
        v = r["vram_sampled"]
        avg = v["alloc_gb"]["mean"] if v["alloc_gb"] else float("nan")
        print(f"{p:12s} {r['weights_gb']:8.3f} {r['load_proc_gb']:10.3f} {avg:7.3f} "
              f"{r['peak_alloc_gb']:7.3f} {r['peak_reserved_gb']:10.3f} "
              f"{r['state_dict_gb']:11.3f} {r['build_peak_gb']:11.3f}")
    if any(runs[p]["vram_sampled"]["proc_gb"] for p in ok):
        print(f"\n{'variant':12s} {'drv avg':>9s} {'drv max':>9s}   (driver view, "
              f"includes CUDA context)")
        for p in ok:
            g = runs[p]["vram_sampled"]["proc_gb"]
            if g:
                print(f"{p:12s} {g['mean']:9.3f} {g['max']:9.3f}")

    print("\n=== latency / speed ===")
    print(f"{'variant':12s} {'load_s':>7s} {'med_s':>7s} {'p90_s':>7s} {'max_s':>7s} "
          f"{'rtf_med':>8s} {'wall_s':>7s} {'vs base':>8s}")
    base = report["runs"].get("bf16")
    for p in ok:
        r = runs[p]
        rel = (r["latency_s"]["median"] / base["latency_s"]["median"]
               if base and "error" not in base else float("nan"))
        print(f"{p:12s} {r['build_s']:7.1f} {r['latency_s']['median']:7.3f} "
              f"{r['latency_s']['p90']:7.3f} {r['latency_s']['max']:7.3f} "
              f"{r['timing_rtf_median']:8.4f} {r['wall_s']:7.0f} {rel:7.2f}x")

    print("\n=== failure modes ===")
    print(f"{'variant':12s} {'empty':>6s} {'runaway':>8s} {'>50% WER clips':>15s}")
    for p in ok:
        f = report["failures"][p]
        print(f"{p:12s} {f['n_empty']:6d} {f['n_runaway']:8d} {f['n_broken_clips']:15d}")

    if report["comparisons"]:
        print("\n=== WER difference vs baseline (paired bootstrap, 95% CI) ===")
        for variant, c in report["comparisons"].items():
            for key in ("wer_diff", "wer_diff_real_only"):
                b = c[key]
                tag = "all" if key == "wer_diff" else "real+degraded"
                print(f"{variant:12s} [{tag:13s}] {b['observed_diff']*100:+7.3f} pp  "
                      f"95% CI [{b['ci95'][0]*100:+.3f}, {b['ci95'][1]*100:+.3f}] pp  "
                      f"p={b['p_two_sided']:.3f}")
            print(f"{'':12s} identical transcripts vs {c['vs']}: "
                  f"{c['identical_after_normalisation']}/{c['of']}")

    for p, r in report["runs"].items():
        if "error" in r:
            print(f"\n[cmp] {p} FAILED: {r['error']}")


if __name__ == "__main__":
    main()
