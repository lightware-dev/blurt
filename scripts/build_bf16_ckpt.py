"""Pre-build the half-precision .nemo checkpoint the ASR server loads onto the GPU.

Downloads the published fp32 Parakeet checkpoint, casts it on CPU, and saves it to
the per-precision cache path the server expects. Running this ahead of time means the
first server start takes the fast load path instead of the one-off download + convert.

Both precisions are also published pre-built and download on first start, so this
script is only needed to work offline, to mirror the weights yourself, or to rebuild
after an upstream change. Each is cast from the upstream fp32 weights.

Usage:
    python scripts/build_bf16_ckpt.py                  # build bf16 if missing
    python scripts/build_bf16_ckpt.py --dtype fp16     # build the fp16 variant
    python scripts/build_bf16_ckpt.py --force          # rebuild even if it exists
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server.asr  # noqa: E402  (applies the NumPy 2 sctypes shim)
from server.asr import PRECISIONS, ParakeetASR  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    # Only the precisions that are a pure cast of the fp32 weights. nvfp4 is not
    # one of them — its scales come from calibrating on real audio, so it has its
    # own builder in scripts/build_nvfp4_snapshot.py.
    ap.add_argument("--dtype", default="bf16",
                    choices=sorted(p for p, spec in PRECISIONS.items()
                                   if spec["kind"] == "nemo"),
                    help="precision to build (default: bf16)")
    ap.add_argument("--force", action="store_true", help="rebuild if checkpoint exists")
    args = ap.parse_args()

    import torch

    asr = ParakeetASR(precision=args.dtype)
    ckpt = Path(asr.ckpt_path())
    if ckpt.exists() and not args.force:
        print(f"already present: {ckpt} ({ckpt.stat().st_size/1e9:.2f} GB) — use --force to rebuild")
        return
    if asr.precision == "bf16" and not torch.cuda.is_bf16_supported():
        print("warning: this GPU does not report bf16 support; the server would not "
              "use this checkpoint. Build --dtype fp16 instead. Building anyway.",
              file=sys.stderr)

    import nemo.collections.asr as nemo_asr

    dtype = asr.torch_dtype()
    t0 = time.time()
    print(f"[build] downloading fp32 {asr.model_name} ...", flush=True)
    model = nemo_asr.models.ASRModel.from_pretrained(asr.model_name, map_location="cpu")
    model.eval()
    # Cast from the upstream fp32 weights, never from the other half-precision
    # checkpoint: bf16 -> fp16 would bake in bf16's coarser mantissa for nothing.
    model.to(dtype)
    print(f"[build] cast to {asr.precision} in {time.time()-t0:.1f}s", flush=True)

    ParakeetASR._save_ckpt(model, str(ckpt))
    print(f"[build] done in {time.time()-t0:.1f}s -> {ckpt} ({ckpt.stat().st_size/1e9:.2f} GB)")


if __name__ == "__main__":
    main()
