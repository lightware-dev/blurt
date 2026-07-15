"""Pre-build the bf16 .nemo checkpoint the ASR server loads directly onto the GPU.

Downloads the published fp32 Parakeet checkpoint, casts it to bf16 on CPU, and
saves it to the per-model cache path the server expects. Running this ahead of
time means the first server start takes the fast bf16 load path instead of the
one-off download + convert.

Usage:
    python scripts/build_bf16_ckpt.py            # build if missing
    python scripts/build_bf16_ckpt.py --force    # rebuild even if it exists
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server.asr  # noqa: E402  (applies the NumPy 2 sctypes shim)
from server.asr import ParakeetASR  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="rebuild if checkpoint exists")
    args = ap.parse_args()

    import torch

    asr = ParakeetASR()
    ckpt = Path(asr.bf16_ckpt_path())
    if ckpt.exists() and not args.force:
        print(f"already present: {ckpt} ({ckpt.stat().st_size/1e9:.2f} GB) — use --force to rebuild")
        return
    if not torch.cuda.is_bf16_supported():
        print("warning: this GPU does not report bf16 support; the server would not "
              "use this checkpoint. Building anyway.", file=sys.stderr)

    import nemo.collections.asr as nemo_asr

    t0 = time.time()
    print(f"[build] downloading fp32 {asr.model_name} ...", flush=True)
    model = nemo_asr.models.ASRModel.from_pretrained(asr.model_name, map_location="cpu")
    model.eval()
    model.to(torch.bfloat16)
    print(f"[build] cast to bf16 in {time.time()-t0:.1f}s", flush=True)

    ParakeetASR._save_bf16_ckpt(model, str(ckpt))
    print(f"[build] done in {time.time()-t0:.1f}s -> {ckpt} ({ckpt.stat().st_size/1e9:.2f} GB)")


if __name__ == "__main__":
    main()
