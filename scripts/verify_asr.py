"""Smoke test: load Parakeet 0.6b-v3, decode a sample wav in-memory, report VRAM/RTF."""

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server.asr import ParakeetASR, SAMPLE_RATE  # noqa: E402


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

    asr = ParakeetASR()
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
