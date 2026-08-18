"""Smoke test: load the configured ASR engine, decode a sample wav in-memory,
report VRAM/RTF.

Runs whichever engine BLURT_ASR_ENGINE selects (Parakeet by default), so the
same numbers can be compared across engines:

    python scripts/verify_asr.py
    BLURT_ASR_ENGINE=whisper python scripts/verify_asr.py
"""

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.asr import SAMPLE_RATE  # noqa: E402
from server.engine import create_asr  # noqa: E402


def load_wav_16k_mono(path: str) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    return audio


def main():
    import torch

    asr = create_asr()
    print(f"[verify] engine={asr.engine} model={asr.model_name} "
          f"precision={asr.precision}", flush=True)
    asr.load()
    if torch.cuda.is_available():
        torch.cuda.reset_peak_memory_stats()

    for name in ["audio/clean.wav", "audio/noisy.wav", "audio/long.wav"]:
        p = Path(name)
        if not p.exists():
            continue
        audio = load_wav_16k_mono(name)
        dur = len(audio) / SAMPLE_RATE
        t0 = time.time()
        text = asr.transcribe(audio)
        dt = time.time() - t0
        print(f"\n[{name}] dur={dur:.1f}s decode={dt*1000:.0f}ms rtf={dt/dur:.4f}")
        print(f"  -> {text!r}")

    if torch.cuda.is_available():
        peak = torch.cuda.max_memory_allocated() / 1e9
        print(f"\n[vram] peak allocated: {peak:.2f} GB")


if __name__ == "__main__":
    main()
