"""
PCM format conversion for incoming audio.

The ASR pipeline wants 16 kHz mono PCM16. Clients declare what they actually
send (rate / width / channels) when a dictation starts; this module converts
each incoming frame to the canonical format. The common case — a client that
already sends 16 kHz mono PCM16 — is a pass-through.

Two properties matter for a stream chopped into arbitrary chunks:

  * A frame may not contain a whole number of samples (a short read, a
    deliberately odd-sized packet). Leftover bytes are carried to the next
    frame rather than crashing the decoder or silently swapping stereo
    channels for the rest of the session.
  * Resampling interpolates *across* the frame boundary. The last output
    sample of a frame usually falls between the frame's final input sample and
    the next frame's first, so the previous frame's tail is retained and that
    output is deferred until the data it needs arrives. Without this the
    boundary degrades to a sample-and-hold — a small click at every frame.

Resampling is linear interpolation, vectorized through np.interp: plenty for
speech into an ASR front-end, and cheap enough to run inline on the event loop.
Clients that can capture at 16 kHz natively should still do so — downsampling
here has no anti-alias filter, so a declared high rate folds content above
8 kHz back into the speech band and costs some accuracy.
"""

from __future__ import annotations

import numpy as np

TARGET_RATE = 16000

# Bounds on what we'll accept rather than silently mis-transcribe.
MIN_RATE, MAX_RATE = 8000, 192000
SUPPORTED_WIDTHS = (2,)          # PCM16 only
SUPPORTED_CHANNELS = (1, 2)      # mono, or stereo (downmixed)


class UnsupportedFormat(ValueError):
    """The declared audio format is outside what convert() can handle."""


def validate(rate: int, width: int, channels: int) -> None:
    """Raise UnsupportedFormat (with a client-presentable message) if unusable."""
    if width not in SUPPORTED_WIDTHS:
        raise UnsupportedFormat(f"unsupported sample width {width} (PCM16 only, width=2)")
    if channels not in SUPPORTED_CHANNELS:
        raise UnsupportedFormat(f"unsupported channel count {channels} (mono or stereo only)")
    if not (MIN_RATE <= rate <= MAX_RATE):
        raise UnsupportedFormat(f"unsupported sample rate {rate} ({MIN_RATE}-{MAX_RATE} Hz)")


class PcmConverter:
    """Streaming converter: declared client format -> 16 kHz mono PCM16 bytes."""

    def __init__(self, rate: int = TARGET_RATE, width: int = 2, channels: int = 1):
        rate, width, channels = int(rate), int(width), int(channels)
        validate(rate, width, channels)
        self.rate = rate
        self.width = width
        self.channels = channels
        self.passthrough = rate == TARGET_RATE and width == 2 and channels == 1
        # bytes per whole frame of input (one sample across all channels)
        self.block = width * channels
        self._byte_tail = b""       # partial block carried from the last call
        # Resampler state. `_pos` is where the next output sample falls, in
        # input-sample units relative to the start of the *next* frame; it goes
        # negative when that output needs the previous frame's final sample,
        # which `_last` retains.
        self._pos = 0.0
        self._last: float | None = None

    def matches(self, rate: int, width: int, channels: int) -> bool:
        return (rate, width, channels) == (self.rate, self.width, self.channels)

    def convert(self, frame: bytes) -> bytes:
        """Convert one frame of the declared format; returns 16 kHz mono PCM16.

        Accepts any byte length: a trailing partial sample is carried to the
        next call, so the caller never has to align its packets.
        """
        if not frame and not self._byte_tail:
            return b""

        if self._byte_tail:
            frame = self._byte_tail + frame
            self._byte_tail = b""
        usable = len(frame) - (len(frame) % self.block)
        if usable != len(frame):
            self._byte_tail = frame[usable:]
            frame = frame[:usable]
        if not frame:
            return b""

        if self.passthrough:
            return frame

        # PCM16 -> float, downmix stereo by averaging the channel pair. The
        # block alignment above guarantees a whole number of channel groups.
        samples = np.frombuffer(frame, dtype=np.int16)
        if self.channels == 2:
            samples = samples.reshape(-1, 2).mean(axis=1)
        samples = samples.astype(np.float32)

        out = samples if self.rate == TARGET_RATE else self._resample(samples)
        if len(out) == 0:
            return b""
        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()

    def _resample(self, samples: np.ndarray) -> np.ndarray:
        """Linear resample with cross-frame carry; may return an empty array."""
        n = len(samples)
        if n == 0:
            return samples

        step = self.rate / TARGET_RATE
        # Prepend the previous frame's last sample so an output landing between
        # frames interpolates correctly; `shift` maps current-frame coordinates
        # into that buffer.
        if self._last is None:
            buf = samples
            shift = 0.0
        else:
            buf = np.concatenate(([np.float32(self._last)], samples))
            shift = 1.0

        # Emit only outputs at positions <= n-1, so interpolation never needs a
        # sample this frame doesn't have. The rest waits for the next frame.
        count = int(np.floor((n - 1 - self._pos) / step)) + 1
        if count <= 0:
            self._pos -= n
            self._last = float(samples[-1])
            return np.empty(0, dtype=np.float32)

        pos = self._pos + step * np.arange(count)
        out = np.interp(pos + shift, np.arange(len(buf), dtype=np.float64), buf)
        self._pos = float(pos[-1] + step - n)
        self._last = float(samples[-1])
        return out.astype(np.float32)
