"""
Blurt — Parakeet dictation server. VAD-segmented streaming over WebSocket.

Protocol
--------
Client -> server:
  * text  {"type": "start"}                 begin a dictation
  * bytes  raw 16 kHz mono PCM16 frames      streamed while dictating
  * text  {"type": "stop"}                  end; server returns the final text

Server -> client:
  * {"type": "status", "state": "ready|loading|error", "detail": ...}
  * {"type": "partial", "text": <running transcript incl. live segment>}
  * {"type": "final",   "text": <full transcript for this dictation>}

Streaming model: audio is split into utterances at silences (Silero VAD). The
active segment is re-decoded every ~PARTIAL_INTERVAL_MS for live partials, and
committed to the transcript on a pause (or when it hits MAX_SEGMENT_S). This
keeps VRAM bounded by the longest single utterance — not the whole session —
while every decode still sees full segment context for low WER.
"""

from __future__ import annotations

import os
import json
import asyncio
from pathlib import Path

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.asr import ParakeetASR, SAMPLE_RATE

# ---- config -------------------------------------------------------------
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "7860"))
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")            # if set, ws requires ?token=
SILENCE_MS = float(os.getenv("SILENCE_MS", "600"))  # pause that commits a segment
PARTIAL_INTERVAL_MS = float(os.getenv("PARTIAL_INTERVAL_MS", "350"))
MAX_SEGMENT_S = float(os.getenv("MAX_SEGMENT_S", "20"))
MIN_SEGMENT_S = float(os.getenv("MIN_SEGMENT_S", "0.3"))
# On stop, re-decode the whole dictation in one pass for the best-possible final
# (avoids segment-boundary artifacts). Beyond this length, fall back to stitching
# the committed segments so a very long dictation doesn't spike VRAM/latency.
FINAL_MAX_S = float(os.getenv("FINAL_MAX_S", "120"))

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

app = FastAPI(title="Blurt — Parakeet dictation server")
asr = ParakeetASR()


def _f32(pcm16: bytes) -> np.ndarray:
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0


async def send_json(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


class Session:
    def __init__(self, ws: WebSocket):
        self.ws = ws
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.vad = None
        self.committed: list[str] = []
        self.running = False
        self.worker: asyncio.Task | None = None

    async def start(self):
        if self.running:
            return
        from server.vad import SileroVAD
        self.vad = SileroVAD()
        self.committed = []
        self.running = True
        # drain any stale frames
        while not self.queue.empty():
            self.queue.get_nowait()
        self.worker = asyncio.create_task(self._run())
        await send_json(self.ws, {"type": "status", "state": "ready"})

    def add_audio(self, pcm16: bytes):
        if self.running:
            self.queue.put_nowait(pcm16)

    async def stop(self):
        if not self.running:
            return
        self.running = False
        self.queue.put_nowait(None)  # sentinel: flush + finalize
        if self.worker:
            try:
                await asyncio.wait_for(self.worker, timeout=10)
            except Exception:
                self.worker.cancel()
        self.worker = None

    def _running_text(self, partial: str = "") -> str:
        parts = [p for p in (self.committed + [partial]) if p]
        return " ".join(parts).strip()

    async def _decode(self, segment: bytearray) -> str:
        audio = _f32(bytes(segment))
        return await asyncio.to_thread(asr.transcribe, audio)

    async def _run(self):
        interval = PARTIAL_INTERVAL_MS / 1000.0
        partial_step = int(PARTIAL_INTERVAL_MS / 1000.0 * SAMPLE_RATE)
        max_seg = int(MAX_SEGMENT_S * SAMPLE_RATE)
        min_seg = int(MIN_SEGMENT_S * SAMPLE_RATE)

        segment = bytearray()      # active (uncommitted) utterance
        full = bytearray()         # entire dictation, for the one-shot final decode
        samples_at_partial = 0
        stopping = False
        max_final = int(FINAL_MAX_S * SAMPLE_RATE)

        # release cached CUDA blocks after decoding a segment longer than this, so a
        # big utterance's activation spike doesn't stay resident (VRAM optimization).
        release_after = int(8 * SAMPLE_RATE)

        async def commit(final_segment: bool):
            nonlocal segment, samples_at_partial
            seg_len = len(segment) // 2
            if seg_len >= min_seg:
                text = await self._decode(segment)
                if text:
                    self.committed.append(text)
            if final_segment or seg_len >= release_after:
                await asyncio.to_thread(asr.release_cache)
            segment = bytearray()
            samples_at_partial = 0
            self.vad.reset()

        try:
            while True:
                try:
                    frame = await asyncio.wait_for(self.queue.get(), timeout=interval)
                except asyncio.TimeoutError:
                    frame = b""  # tick: re-evaluate silence/partial even without audio

                if frame is None:  # stop sentinel
                    stopping = True
                elif frame:
                    segment.extend(frame)
                    full.extend(frame)
                    self.vad.process(_f32(frame))

                seg_samples = len(segment) // 2

                if stopping:
                    # Best final: one full-context decode of the whole dictation.
                    final_text = ""
                    if 0 < len(full) // 2 <= max_final:
                        final_text = await self._decode(full)
                        await asyncio.to_thread(asr.release_cache)
                    if not final_text:  # too long (or empty) → stitch committed segments
                        await commit(final_segment=True)
                        final_text = self._running_text()
                    await send_json(self.ws, {"type": "final", "text": final_text})
                    return

                # commit at a natural pause or when the segment gets too long
                if self.vad.saw_speech and self.vad.silence_ms >= SILENCE_MS and seg_samples >= min_seg:
                    await commit(final_segment=False)
                    await send_json(self.ws, {"type": "partial", "text": self._running_text()})
                    continue
                if seg_samples >= max_seg:
                    await commit(final_segment=False)
                    await send_json(self.ws, {"type": "partial", "text": self._running_text()})
                    continue

                # live partial for the active segment
                if seg_samples >= min_seg and seg_samples - samples_at_partial >= partial_step:
                    text = await self._decode(segment)
                    samples_at_partial = seg_samples
                    await send_json(self.ws, {"type": "partial", "text": self._running_text(text)})
        except asyncio.CancelledError:
            pass
        except Exception as e:
            await send_json(self.ws, {"type": "status", "state": "error", "detail": str(e)})


@app.get("/")
async def index():
    return FileResponse(HERE.parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if AUTH_TOKEN and ws.query_params.get("token") != AUTH_TOKEN:
        await ws.close(code=1008)
        return
    await ws.accept()
    sess = Session(ws)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                sess.add_audio(msg["bytes"])
            elif msg.get("text") is not None:
                cmd = json.loads(msg["text"]).get("type")
                if cmd == "start":
                    await sess.start()
                elif cmd == "stop":
                    await sess.stop()
    except WebSocketDisconnect:
        pass
    finally:
        await sess.stop()
