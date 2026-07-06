"""
Streaming voice-activity detection via Silero VAD.

Runs on CPU (the model is tiny) so it never contends with Parakeet on the GPU.
Feeds arbitrary-length 16 kHz float32 frames in, tracks contiguous speech and
silence run-lengths, so the session layer can decide when an utterance has ended
(a pause) and should be committed.
"""

from __future__ import annotations

import numpy as np

SAMPLE_RATE = 16000
WINDOW = 512  # Silero expects 512-sample (32 ms) windows at 16 kHz


class SileroVAD:
    def __init__(self, threshold: float = 0.5):
        import torch  # noqa: F401
        from silero_vad import load_silero_vad

        self._torch = __import__("torch")
        self.model = load_silero_vad()  # jit, CPU
        self.threshold = threshold
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

    def process(self, frame_f32: np.ndarray) -> None:
        """Update speech/silence run-lengths from a new float32 frame."""
        buf = np.concatenate([self._tail, frame_f32]) if self._tail.size else frame_f32
        n_win = len(buf) // WINDOW
        for i in range(n_win):
            win = buf[i * WINDOW:(i + 1) * WINDOW]
            t = self._torch.from_numpy(np.ascontiguousarray(win))
            with self._torch.no_grad():
                prob = float(self.model(t, SAMPLE_RATE).item())
            if prob >= self.threshold:
                self.saw_speech = True
                self.speech_run += WINDOW
                self.silence_run = 0
            else:
                self.silence_run += WINDOW
                self.speech_run = 0
        self._tail = buf[n_win * WINDOW:].copy()

    @property
    def silence_ms(self) -> float:
        return 1000.0 * self.silence_run / SAMPLE_RATE
