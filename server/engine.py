"""
Engine selection — which ASR model this process runs.

Blurt ships two: NVIDIA **Parakeet** (server/asr.py, the default) and OpenAI
**Whisper** (server/whisper.py). Exactly one is loaded per process, chosen at
startup with BLURT_ASR_ENGINE or `blurtd --engine`, and everything downstream —
the WebSocket session, the Wyoming listener, the OpenAI-compatible API — holds
it through the single `asr` global in server/app.py without knowing which it
got. They are deliberately not loadable side by side: two models resident would
double the VRAM the whole design is built to keep small, and a per-request
choice would mean a reload between requests.

The engines are duck-typed rather than sharing a base class, because the surface
is four calls wide and an ABC would be more ceremony than contract:

    engine / model_name / description / attribution / languages / model_version
    precision, dtype                 what the weights are in
    is_loaded                        resident yet?
    load()                           idempotent; raises with an actionable message
    transcribe(float32 16 kHz) -> str
    release_cache()                  hand cached CUDA blocks back to the driver

`transcribe` is synchronous and internally locked; callers run it in a worker
thread. Anything added here has to exist on both engines — the callers have no
way to ask which one they are holding.
"""

from __future__ import annotations

import os

PARAKEET = "parakeet"
WHISPER = "whisper"
ENGINES = (PARAKEET, WHISPER)
DEFAULT_ENGINE = PARAKEET

# Accepted spellings, so BLURT_ASR_ENGINE takes the obvious names.
_ENGINE_ALIASES = {
    "parakeet": PARAKEET, "nemo": PARAKEET, "nvidia": PARAKEET,
    "whisper": WHISPER, "openai": WHISPER,
}


def resolve_engine(name: str | None) -> str:
    """Normalise an engine name ('nemo' -> 'parakeet'); None/'' means the default."""
    if not name:
        return DEFAULT_ENGINE
    key = _ENGINE_ALIASES.get(str(name).strip().lower())
    if key is None:
        raise ValueError(
            f"Unsupported BLURT_ASR_ENGINE {name!r}; expected one of {list(ENGINES)}.")
    return key


def create_asr(engine: str | None = None):
    """Build the engine this process will serve from.

    Imports the chosen module only — the two pull in different halves of a heavy
    dependency tree (NeMo against transformers), and a server running one has no
    reason to pay the other's import.
    """
    name = resolve_engine(engine or os.getenv("BLURT_ASR_ENGINE"))
    if name == WHISPER:
        from server.whisper import WhisperASR

        return WhisperASR()
    from server.asr import ParakeetASR

    return ParakeetASR()
