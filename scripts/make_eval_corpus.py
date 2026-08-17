#!/usr/bin/env python3
"""Build the evaluation corpus scripts/compare_precision.py decodes.

Three kinds of material, because each catches a different failure:
  * real speech  — LibriSpeech test-clean utterances (ground-truth transcripts),
                   the only honest source of WER;
  * degraded     — the same utterances plus white / babble noise at fixed SNR and
                   a loud (near-clipping) copy, which is where a narrow-exponent
                   format would break first;
  * synthetic    — espeak-ng clips and the repo's audio/*.wav, easy material that
                   keeps the numbers comparable with the existing smoke test.

Everything lands as 16 kHz mono float wav plus a manifest.json of
{id, path, text, kind, seconds}. Reference text is normalised the way ASR eval
normally does (lowercase, no punctuation) at scoring time, not here.

Usage:
    python scripts/make_eval_corpus.py --parquet test-clean.parquet --out /tmp/corpus
    python scripts/make_eval_corpus.py --out /tmp/corpus --limit 40   # smaller run
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

SAMPLE_RATE = 16000

# Held-out sentences for the espeak-ng half. Deliberately varied: digits, proper
# nouns, hesitation, and a long compound sentence — the shapes dictation actually
# sees, which LibriSpeech's read prose does not cover.
SYNTH_SENTENCES = {
    "synth-digits": "Set a timer for fifteen minutes and remind me at four thirty.",
    "synth-names": "Send the draft to Priya Raghavan and copy Miguel Vitorino.",
    "synth-tech": "The transformer encoder runs in half precision on the GPU.",
    "synth-long": (
        "Machine learning has changed automatic speech recognition, and modern "
        "systems transcribe speech with remarkable accuracy across accents, "
        "dialects, and noisy rooms, which is why the word error rate keeps falling."
    ),
    "synth-short": "Yes.",
}

# The repo's own smoke-test wavs, with the reference text they were generated from
# (scripts/generate_samples.py).
REPO_WAVS = {
    "repo-clean": (
        "audio/clean.wav",
        "The quick brown fox jumps over the lazy dog. Speech recognition is a "
        "fascinating field of artificial intelligence."),
    "repo-noisy": (
        "audio/noisy.wav",
        "Hey Siri, set a timer for fifteen minutes and remind me to check the oven "
        "at four thirty PM."),
    "repo-long": (
        "audio/long.wav",
        "Machine learning has revolutionized the field of automatic speech "
        "recognition. Modern systems like NVIDIA Parakeet and OpenAI Whisper use "
        "deep neural networks trained on massive amounts of multilingual data. "
        "These models can transcribe speech with remarkable accuracy, handle "
        "various accents and dialects, and even work in noisy environments. The "
        "key innovation has been the transformer architecture, which allows the "
        "model to attend to the full audio context simultaneously rather than "
        "processing it sequentially. This has led to dramatic improvements in word "
        "error rates across many languages and domains."),
}


def write_wav(path: Path, audio: np.ndarray) -> float:
    """Write mono float32 16 kHz audio; returns duration in seconds."""
    audio = np.asarray(audio, dtype=np.float32)
    sf.write(str(path), audio, SAMPLE_RATE, subtype="FLOAT")
    return len(audio) / SAMPLE_RATE


def mix_at_snr(speech: np.ndarray, noise: np.ndarray, snr_db: float) -> np.ndarray:
    """Add noise to speech at a target SNR, then guard against clipping."""
    if len(noise) < len(speech):
        noise = np.tile(noise, int(np.ceil(len(speech) / len(noise))))
    noise = noise[:len(speech)]
    sp = float(np.mean(speech.astype(np.float64) ** 2)) or 1e-12
    npow = float(np.mean(noise.astype(np.float64) ** 2)) or 1e-12
    scale = np.sqrt(sp / (npow * (10 ** (snr_db / 10.0))))
    mixed = speech + scale * noise
    peak = float(np.max(np.abs(mixed))) or 1.0
    if peak > 0.99:
        mixed = mixed * (0.99 / peak)
    return mixed.astype(np.float32)


def load_librispeech(parquet: Path, limit: int) -> list[tuple[str, np.ndarray, str]]:
    """Read (id, audio, text) triples out of a LibriSpeech parquet shard.

    Utterances are taken spread across the shard rather than from the front, so the
    sample covers several speakers instead of one reader's first few sentences.
    """
    import pyarrow.parquet as pq

    table = pq.read_table(str(parquet), columns=["id", "audio", "text"])
    n = table.num_rows
    idx = np.linspace(0, n - 1, num=min(limit, n)).astype(int)
    ids, audios, texts = table["id"], table["audio"], table["text"]
    out = []
    for i in idx:
        i = int(i)
        blob = audios[i].as_py()
        raw = blob["bytes"] if isinstance(blob, dict) else blob
        audio, sr = sf.read(io.BytesIO(raw), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != SAMPLE_RATE:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
        out.append((str(ids[i].as_py()), audio, str(texts[i].as_py())))
    return out


def espeak(text: str, name: str, cache_dir: Path | None) -> np.ndarray | None:
    """Render text with espeak-ng at 16 kHz mono.

    Falls back to a pre-rendered `<cache_dir>/<name>.wav`, since the ASR container
    that has torch and NeMo has no espeak-ng in it — render those on the host with
    `espeak-ng -s 150 -w <name>.wav "<text>"` and point --espeak-dir here.
    Returns None when neither source is available.
    """
    cached = (cache_dir / f"{name}.wav") if cache_dir else None
    if cached is not None and cached.exists():
        wav = cached.read_bytes()
    elif shutil.which("espeak-ng") is not None:
        wav = subprocess.run(
            ["espeak-ng", "-s", "150", "--stdout", text],
            check=True, capture_output=True).stdout
    else:
        return None
    audio, sr = sf.read(io.BytesIO(wav), dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    pad = np.zeros(SAMPLE_RATE, dtype=np.float32)  # a second of silence each end
    return np.concatenate([pad, audio, pad])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="corpus directory to write")
    ap.add_argument("--parquet", help="LibriSpeech test-clean parquet shard")
    ap.add_argument("--limit", type=int, default=100, help="real-speech utterances")
    ap.add_argument("--noisy", type=int, default=25,
                    help="how many of them also get noisy/loud variants")
    ap.add_argument("--espeak-dir", help="dir of pre-rendered espeak wavs (see espeak())")
    ap.add_argument("--seed", type=int, default=1234)
    args = ap.parse_args()

    rng = np.random.default_rng(args.seed)
    out = Path(args.out)
    (out / "wav").mkdir(parents=True, exist_ok=True)
    items = []

    real = load_librispeech(Path(args.parquet), args.limit) if args.parquet else []
    if not real:
        print("warn: no real speech included (no --parquet)", file=sys.stderr)

    for uid, audio, text in real:
        p = out / "wav" / f"ls-{uid}.wav"
        items.append({"id": f"ls-{uid}", "path": str(p), "text": text,
                      "kind": "real", "seconds": write_wav(p, audio)})

    # Babble = a few real utterances summed, which is far closer to the noise a
    # dictation mic picks up than white noise alone.
    babble = None
    if len(real) >= 4:
        take = [a for _, a, _ in real[:6]]
        n = max(len(a) for a in take)
        babble = np.sum([np.pad(a, (0, n - len(a))) for a in take], axis=0)
        babble /= max(float(np.max(np.abs(babble))), 1e-9)

    for uid, audio, text in real[:args.noisy]:
        white = rng.standard_normal(len(audio)).astype(np.float32)
        variants = {
            "white10": mix_at_snr(audio, white, 10.0),
            "white05": mix_at_snr(audio, white, 5.0),
            # Near full scale: the mel energies scale with amplitude, so this is the
            # case that would overflow a narrow-range dtype if anything does.
            "loud": (audio / max(float(np.max(np.abs(audio))), 1e-9) * 0.995).astype(np.float32),
        }
        if babble is not None:
            variants["babble05"] = mix_at_snr(audio, babble, 5.0)
        for tag, wave in variants.items():
            p = out / "wav" / f"ls-{uid}-{tag}.wav"
            items.append({"id": f"ls-{uid}-{tag}", "path": str(p), "text": text,
                          "kind": f"degraded-{tag}", "seconds": write_wav(p, wave)})

    espeak_dir = Path(args.espeak_dir) if args.espeak_dir else None
    for name, text in SYNTH_SENTENCES.items():
        audio = espeak(text, name, espeak_dir)
        if audio is None:
            print("warn: no espeak-ng and no --espeak-dir, skipping synthetic clips",
                  file=sys.stderr)
            break
        p = out / "wav" / f"{name}.wav"
        items.append({"id": name, "path": str(p), "text": text,
                      "kind": "synthetic", "seconds": write_wav(p, audio)})

    repo_root = Path(__file__).resolve().parent.parent
    for name, (rel, text) in REPO_WAVS.items():
        src = repo_root / rel
        if not src.exists():
            continue
        audio, sr = sf.read(str(src), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != SAMPLE_RATE:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
        p = out / "wav" / f"{name}.wav"
        items.append({"id": name, "path": str(p), "text": text,
                      "kind": "synthetic", "seconds": write_wav(p, audio)})

    manifest = out / "manifest.json"
    manifest.write_text(json.dumps(items, indent=2))
    total = sum(i["seconds"] for i in items)
    by_kind = {}
    for i in items:
        by_kind[i["kind"]] = by_kind.get(i["kind"], 0) + 1
    print(f"wrote {len(items)} clips ({total/60:.1f} min) -> {manifest}")
    for k in sorted(by_kind):
        print(f"  {k:20s} {by_kind[k]}")


if __name__ == "__main__":
    main()
