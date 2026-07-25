"""
Streaming voice-activity detection via Silero VAD.

Runs on CPU (the model is tiny) so it never contends with Parakeet on the GPU.
Feeds arbitrary-length 16 kHz float32 frames in, tracks contiguous speech and
silence run-lengths, so the session layer can decide when an utterance has ended
(a pause) and should be committed.

`process` also *returns* the speech it kept, which is what the session buffers
for decoding. Parakeet has no VAD of its own: hand it a room's background
chatter and it will faithfully turn it into words, so audio the VAD rejects
must never reach it. Two paddings keep that gate from eating real speech:

  * pre-roll — Silero needs a few tens of ms of a word before it crosses the
    threshold, so the gate opens *after* the onset. We hold the last
    `preroll_ms` of rejected audio and prepend it when speech starts, or
    "send" arrives at the model as "end".
  * hangover — trailing consonants decay below the threshold while still
    carrying the word (the release of a final /t/, an unvoiced fricative), so
    the gate stays open `hangover_ms` past the last speech window.
"""

from __future__ import annotations

import warnings

import numpy as np

# Silero VAD's LSTM weights aren't stored contiguously; torch warns about a minor
# recompaction cost on every call. Cosmetic for a model this tiny — silence it.
warnings.filterwarnings(
    "ignore",
    message=r".*RNN module weights are not part of single contiguous chunk.*",
)

SAMPLE_RATE = 16000
WINDOW = 512  # Silero expects 512-sample (32 ms) windows at 16 kHz


EMPTY = np.zeros(0, dtype=np.float32)


class SpeechGate:
    """Decides which audio survives, given per-chunk speech/non-speech verdicts.

    Split out from SileroVAD because it holds no model and needs no torch: the
    padding rules are the part with edge cases worth testing directly (a gate
    that opens late eats word onsets, one that never closes defeats the point).
    Chunks are whatever the caller classifies — 512-sample windows for Silero.
    """

    def __init__(self, preroll_ms: float = 250.0, hangover_ms: float = 200.0):
        self._preroll_max = int(preroll_ms / 1000.0 * SAMPLE_RATE)
        self._hangover_max = int(hangover_ms / 1000.0 * SAMPLE_RATE)
        self.reset()

    def reset(self):
        self._preroll: list[np.ndarray] = []  # rejected chunks held for the next onset
        self._preroll_len = 0
        self._hangover = 0                    # samples of grace left after speech

    @property
    def open(self) -> bool:
        """True while audio is still being kept — speech or its hangover."""
        return self._hangover > 0

    def push(self, chunk: np.ndarray, speech: bool) -> list[np.ndarray]:
        """Return the chunks to keep for this verdict (possibly none, or —
        at an onset — the held pre-roll ahead of the chunk itself)."""
        if speech:
            out = self._preroll + [chunk]
            self._preroll = []
            self._preroll_len = 0
            self._hangover = self._hangover_max
            return out
        if self._hangover > 0:
            self._hangover -= len(chunk)
            return [chunk]
        self._preroll.append(chunk)
        self._preroll_len += len(chunk)
        # Drop from the front: the pre-roll is a ring of the *most recent*
        # rejected audio, so an hour of silence still costs only preroll_ms.
        while self._preroll_len > self._preroll_max and self._preroll:
            self._preroll_len -= len(self._preroll.pop(0))
        return []


class SileroVAD:
    def __init__(self, threshold: float = 0.5,
                 preroll_ms: float = 250.0, hangover_ms: float = 200.0):
        import torch  # noqa: F401
        from silero_vad import load_silero_vad

        self._torch = __import__("torch")
        self.model = load_silero_vad()  # jit, CPU
        self.threshold = threshold
        self.gate = SpeechGate(preroll_ms, hangover_ms)
        self._tail = np.zeros(0, dtype=np.float32)
        # run-lengths in samples of the current contiguous speech / silence stretch
        self.speech_run = 0
        self.silence_run = 0
        self.saw_speech = False  # any speech seen since last reset

    def reset(self):
        try:
            self.model.reset_states()
        except Exception:
            pass
        self._tail = np.zeros(0, dtype=np.float32)
        self.speech_run = 0
        self.silence_run = 0
        self.saw_speech = False
        self.gate.reset()

    def process(self, frame_f32: np.ndarray) -> np.ndarray:
        """Update run-lengths from a new frame; return the audio worth decoding.

        The return value is the frame minus its rejected stretches, plus any
        pre-roll owed to a speech onset — so it can be longer than the input on
        the window that opens the gate, and empty for a silent room.
        """
        buf = np.concatenate([self._tail, frame_f32]) if self._tail.size else frame_f32
        n_win = len(buf) // WINDOW
        keep: list[np.ndarray] = []
        for i in range(n_win):
            win = buf[i * WINDOW:(i + 1) * WINDOW]
            t = self._torch.from_numpy(np.ascontiguousarray(win))
            with self._torch.no_grad():
                prob = float(self.model(t, SAMPLE_RATE).item())
            speech = prob >= self.threshold
            if speech:
                self.saw_speech = True
                self.speech_run += WINDOW
                self.silence_run = 0
            else:
                self.silence_run += WINDOW
                self.speech_run = 0
            keep.extend(self.gate.push(win, speech))
        self._tail = buf[n_win * WINDOW:].copy()
        if not keep:
            return EMPTY
        return np.concatenate(keep) if len(keep) > 1 else keep[0]

    def flush(self) -> np.ndarray:
        """Release the sub-window remainder at end of dictation.

        `process` can only classify whole 512-sample windows, so up to 32 ms of
        audio is always held back. Stopping mid-word would otherwise clip it —
        harmless for a silent room, but the tail of the last word is exactly
        what the final decode needs. Released only if the gate is open.
        """
        tail, self._tail = self._tail, np.zeros(0, dtype=np.float32)
        # speech_run covers hangover_ms=0, where the gate shuts the instant a
        # window ends and `open` would drop a tail we're still mid-word on.
        if tail.size and (self.speech_run > 0 or self.gate.open):
            return tail
        return EMPTY

    @property
    def silence_ms(self) -> float:
        return 1000.0 * self.silence_run / SAMPLE_RATE
