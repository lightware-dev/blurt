#!/usr/bin/env python3
"""Generate test audio samples for STT benchmarking."""

import subprocess
from pathlib import Path

SAMPLES = {
    "clean": "The quick brown fox jumps over the lazy dog. Speech recognition is a fascinating field of artificial intelligence.",
    "noisy": "Hey Siri, set a timer for fifteen minutes and remind me to check the oven at four thirty PM.",
    "long": (
        "Machine learning has revolutionized the field of automatic speech recognition. "
        "Modern systems like NVIDIA Parakeet and OpenAI Whisper use deep neural networks "
        "trained on massive amounts of multilingual data. These models can transcribe speech "
        "with remarkable accuracy, handle various accents and dialects, and even work in "
        "noisy environments. The key innovation has been the transformer architecture, "
        "which allows the model to attend to the full audio context simultaneously rather "
        "than processing it sequentially. This has led to dramatic improvements in word "
        "error rates across many languages and domains."
    ),
}

OUTPUT_DIR = Path("audio")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    for name, text in SAMPLES.items():
        out = OUTPUT_DIR / f"{name}.aiff"
        print(f"🔊 Generating {out}...")
        subprocess.run(
            ["say", text, "-o", str(out), "-r", "170"],
            check=True,
        )
        # Convert to WAV for wider compat
        wav = OUTPUT_DIR / f"{name}.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(out), "-ar", "16000", "-ac", "1", str(wav)],
            check=True,
            capture_output=True,
        )
        out.unlink()  # remove aiff
        print(f"   → {wav}")

    # Print reference texts
    print("\n📝 Reference texts (copy these for --ref):")
    for name, text in SAMPLES.items():
        print(f"\n--- {name} ---")
        print(text)


if __name__ == "__main__":
    main()
