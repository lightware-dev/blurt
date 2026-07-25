"""
Blurt — Parakeet dictation server. VAD-segmented streaming over WebSocket.

Protocol (full reference in docs/protocol.md)
---------------------------------------------
Client -> server:
  * text   {"type": "describe"}              ask for an info message
  * text   {"type": "start", "id": ..., "audio": {rate,width,channels}}
  * bytes  PCM frames in the declared format  streamed while dictating
  * text   {"type": "stop", "id": ...}       end; server returns the final text

Server -> client:
  * {"type": "info", "protocol": 1, "model": ..., "state": ..., "audio": {...}}
  * {"type": "status", "state": "ready|loading|error", "detail": ..., "id": ...}
  * {"type": "vad",     "speech": true|false, "id": ...}
  * {"type": "partial", "text": ..., "committed": ..., "live": ..., "id": ...}
  * {"type": "final",   "text": ..., "id": ...}

Every dictation-scoped message carries the dictation `id` (client-chosen in
`start`, server-generated otherwise) so late messages from a finished dictation
can be dropped. Both sides ignore unknown message types and fields, and `id`
and `audio` are optional in `start`, so the two ends can evolve independently.

An optional second listener speaks the Wyoming protocol (server/wyoming.py) so
Blurt can serve as a Home Assistant STT backend. It is OFF by default because
Wyoming has neither auth nor TLS; WYOMING_PORT=10300 opts in.
The same port as the WebSocket also serves an OpenAI-compatible transcription
API (server/openai_api.py): POST /v1/audio/transcriptions, GET /v1/models.

Streaming model: the Silero VAD both gates and segments the audio. Only what it
scores as speech is buffered — Parakeet has no VAD of its own, so background
chatter that reaches it comes back as words — and the buffer is split into
utterances at silences. The active segment is re-decoded every
~PARTIAL_INTERVAL_MS for live partials, and committed to the transcript on a
pause (or when it hits MAX_SEGMENT_S). This keeps VRAM bounded by the longest
single utterance — not the whole session — while every decode still sees full
segment context for low WER.
"""

from __future__ import annotations

import os
import json
import time
import uuid
import hmac
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from server.asr import ParakeetASR, SAMPLE_RATE
from server.pcm import PcmConverter, UnsupportedFormat

# ---- config -------------------------------------------------------------
SERVER_VERSION = "0.1"
# Bumped only for a breaking protocol change; additive fields don't move it.
PROTOCOL_VERSION = 1

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "25878"))
AUTH_TOKEN = os.getenv("AUTH_TOKEN", "")            # if set, ws requires ?token=
# Wyoming listener (Home Assistant STT), OFF by default. Wyoming has no auth and
# no TLS, so an open port here would bypass AUTH_TOKEN entirely — opting in is a
# deliberate choice. Set WYOMING_PORT=10300 (the ecosystem's ASR convention) to
# enable, and prefer binding it to a specific interface rather than 0.0.0.0.
WYOMING_PORT = int(os.getenv("WYOMING_PORT", "0"))
# Interface for the Wyoming listener; defaults to HOST but can be narrowed
# (e.g. WYOMING_HOST=127.0.0.1) without moving the WebSocket off the LAN.
WYOMING_HOST = os.getenv("WYOMING_HOST", "") or HOST
# Per-dictation metadata logging (packets/bytes/duration — never transcript text).
LOG_STATS = os.getenv("LOG_STATS", "1").strip().lower() not in ("", "0", "false", "no", "off")
SILENCE_MS = float(os.getenv("SILENCE_MS", "600"))  # pause that commits a segment
PARTIAL_INTERVAL_MS = float(os.getenv("PARTIAL_INTERVAL_MS", "350"))
MAX_SEGMENT_S = float(os.getenv("MAX_SEGMENT_S", "20"))
MIN_SEGMENT_S = float(os.getenv("MIN_SEGMENT_S", "0.3"))
# On stop, re-decode the whole dictation in one pass for the best-possible final
# (avoids segment-boundary artifacts). Beyond this length, fall back to stitching
# the committed segments so a very long dictation doesn't spike VRAM/latency.
FINAL_MAX_S = float(os.getenv("FINAL_MAX_S", "120"))
# Debounce for vad speech=false events: silence must persist this long before we
# report the user stopped speaking (speech=true is reported immediately).
VAD_OFF_MS = float(os.getenv("VAD_OFF_MS", "300"))
# How sure Silero must be before audio counts as speech — and so before any of
# it reaches the model. Raise it (0.6-0.7) when a noisy room's background voices
# still get transcribed; lower it if your own quiet speech goes missing. Raising
# it also delays the onset, so it pairs with a larger VAD_PREROLL_MS.
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.5"))
# Padding around the speech the VAD keeps. Silero crosses its threshold a beat
# after a word starts and drops below it while the last consonant is still
# sounding, so gating on the raw decision clips both ends. Raise these if words
# come back missing their first or last phoneme; lower them if too much room
# tone survives the gate.
VAD_PREROLL_MS = float(os.getenv("VAD_PREROLL_MS", "250"))
VAD_HANGOVER_MS = float(os.getenv("VAD_HANGOVER_MS", "200"))
# How long `stop` waits for the final decode before giving up and sending the
# best text it already has. Generous on purpose: a decode that is merely slow
# (a long dictation, or GPU contention with a /v1 request) must not silently
# cost the user their transcript.
STOP_TIMEOUT_S = float(os.getenv("STOP_TIMEOUT_S", "60"))
# Cap on queued-but-undecoded audio per dictation. A client streaming faster
# than realtime (or a decode that stalls) would otherwise grow this without
# bound; at ~32 KB/s of PCM this is several minutes of backlog.
MAX_QUEUED_FRAMES = int(os.getenv("MAX_QUEUED_FRAMES", "4096"))

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

asr = ParakeetASR()


def server_info() -> dict:
    """The `info` message: who we are, what we run, what audio we canonically want.

    `audio` is the server's native format; clients may declare a different one in
    `start` and the server converts (see server/pcm.py).
    """
    return {
        "type": "info",
        "protocol": PROTOCOL_VERSION,
        "server": "blurtd",
        "version": SERVER_VERSION,
        "model": asr.model_name,
        "state": "ready" if asr.is_loaded else "loading",
        "audio": {"rate": SAMPLE_RATE, "width": 2, "channels": 1},
    }


def _f32(pcm16: bytes) -> np.ndarray:
    return np.frombuffer(pcm16, dtype=np.int16).astype(np.float32) / 32768.0


def _pcm16(f32: np.ndarray) -> bytes:
    """Inverse of _f32. Exact for anything that came from PCM16 in the first
    place — the /32768 scaling is lossless in both directions."""
    return np.clip(np.rint(f32 * 32768.0), -32768, 32767).astype(np.int16).tobytes()


def _log(msg: str):
    # Metadata only — packet/byte/duration counters, never transcript text.
    if LOG_STATS:
        print(f"[blurtd] {msg}", flush=True)


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{int(n)} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} MB"


class Session:
    """One dictation pipeline: PCM in, VAD segmentation, partial/final events out.

    Transport-agnostic: `emit` is an async callable that receives protocol
    events as dicts (the native WebSocket handler serializes them as-is; the
    Wyoming listener translates them). Every event carries the dictation id.
    """

    def __init__(self, emit: Callable[[dict], Awaitable[None]]):
        self.emit = emit
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.vad = None
        self.committed: list[str] = []
        self.running = False
        self.worker: asyncio.Task | None = None
        self.dictation_id: str = ""
        self.converter = PcmConverter()
        # Bumped on every start/abort. A worker carries the generation it was
        # created under and its events are dropped once that no longer matches,
        # so an orphaned worker can neither emit under a successor's id nor
        # touch its state — which is what lets abort() detach instead of
        # awaiting a decode thread it cannot interrupt.
        self.generation = 0
        # per-dictation metadata counters (metadata only — no transcript text)
        self.packets = 0
        self.bytes_in = 0
        self.t_start = 0.0
        self._speech_on = False
        self._final_sent = False
        self._dropped = 0

    async def start(self, dictation_id: str | None = None,
                    converter: PcmConverter | None = None):
        # A second `start` supersedes the one in flight. Ignoring it would
        # leave the session emitting events under the *previous* id — which the
        # client drops as stale — and make the new id's `stop` unmatchable,
        # wedging the connection with neither a final nor an error.
        if self.running:
            _log("start during an active dictation — superseding it")
            self.abort()
        from server.vad import SileroVAD
        self.vad = SileroVAD(threshold=VAD_THRESHOLD,
                             preroll_ms=VAD_PREROLL_MS, hangover_ms=VAD_HANGOVER_MS)
        self.committed = []
        self.dictation_id = dictation_id or uuid.uuid4().hex
        self.converter = converter or PcmConverter()
        self.packets = 0
        self.bytes_in = 0
        self.t_start = time.monotonic()
        self._speech_on = False
        self._final_sent = False
        self._dropped = 0
        self.converter._byte_tail = b""
        self.generation += 1
        self.running = True
        # drain any stale frames
        while not self.queue.empty():
            self.queue.get_nowait()
        self.worker = asyncio.create_task(self._run(self.generation))
        _log("dictation started")
        await self._emit({"type": "status", "state": "ready"})

    def add_audio(self, frame: bytes):
        """Queue one frame of client audio (in the declared format).

        Malformed input is dropped, never raised: this runs inline on the
        transport's receive loop, so an exception here would kill the whole
        connection over one bad packet.
        """
        if not self.running:
            return
        self.packets += 1
        self.bytes_in += len(frame)
        if self.queue.qsize() >= MAX_QUEUED_FRAMES:
            self._dropped += 1
            return
        try:
            pcm16 = self.converter.convert(frame)
        except Exception:
            self._dropped += 1
            return
        if pcm16:
            self.queue.put_nowait(pcm16)

    async def stop(self):
        """Finalize: flush, decode the best final text, emit it.

        Always emits exactly one `final` — a slow or wedged decode degrades to
        the text already committed rather than silently costing the user their
        dictation.
        """
        if not self.running:
            return
        self.running = False
        self.queue.put_nowait(None)  # sentinel: flush + finalize
        if self.worker:
            try:
                await asyncio.wait_for(self.worker, timeout=STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                _log(f"final decode exceeded {STOP_TIMEOUT_S:.0f}s — "
                     "returning the committed text")
            except Exception:
                self.worker.cancel()
        self.worker = None
        if not self._final_sent:
            # The worker was cancelled or died before emitting. Send the best
            # text we have so the client is never left waiting on a final.
            await self._emit({"type": "final", "text": self._texts()[2]})
            self._final_sent = True

    def abort(self):
        """Tear down without finalizing — no decode, no `final` event.

        Used when the transport drops mid-dictation, or when a new `start`
        supersedes the one in flight. Deliberately does not await the worker: a
        task cancelled inside a decode thread only unwinds when that decode
        returns, so awaiting could stall teardown for seconds and forever on a
        wedged CUDA context. Bumping the generation orphans the worker instead
        — its events are dropped and it touches no state the successor uses.
        """
        if not self.running:
            return
        self.running = False
        self.generation += 1
        if self.worker:
            self.worker.cancel()
        self.worker = None
        _log("dictation aborted")

    async def _emit(self, payload: dict, generation: int | None = None):
        """Emit one protocol event, unless it belongs to a superseded dictation."""
        if generation is not None and generation != self.generation:
            return
        payload.setdefault("id", self.dictation_id)
        await self.emit(payload)

    def _texts(self, partial: str = "") -> tuple[str, str, str]:
        """(committed, live, full) — full is committed + live joined."""
        committed = " ".join(p for p in self.committed if p).strip()
        live = partial.strip()
        full = " ".join(t for t in (committed, live) if t)
        return committed, live, full

    async def _emit_partial(self, live_text: str = "", generation: int | None = None):
        committed, live, full = self._texts(live_text)
        await self._emit({"type": "partial", "text": full,
                          "committed": committed, "live": live}, generation)

    async def _update_vad_state(self, generation: int | None = None):
        """Report speech on/off transitions. `on` fires immediately; `off` only
        after VAD_OFF_MS of silence so brief inter-word gaps don't flap. Segment
        commits reset the VAD's counters, so state is tracked here, not there."""
        if not self._speech_on and self.vad.speech_run > 0:
            self._speech_on = True
            await self._emit({"type": "vad", "speech": True}, generation)
        elif self._speech_on and self.vad.silence_ms >= VAD_OFF_MS:
            self._speech_on = False
            await self._emit({"type": "vad", "speech": False}, generation)

    async def _decode(self, segment: bytearray) -> str:
        audio = _f32(bytes(segment))
        return await asyncio.to_thread(asr.transcribe, audio)

    async def _run(self, generation: int):
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
                    # Whole-window classification always holds back <32 ms;
                    # if the user stopped mid-word, that remainder is the end
                    # of their last word.
                    tail = self.vad.flush()
                    if tail.size:
                        pcm = _pcm16(tail)
                        segment.extend(pcm)
                        full.extend(pcm)
                elif frame:
                    # Buffer what the VAD kept, not what arrived. Parakeet has
                    # no VAD of its own, so anything that reaches it becomes
                    # words — including a neighbour's conversation the VAD
                    # correctly scored as non-speech.
                    speech = self.vad.process(_f32(frame))
                    await self._update_vad_state(generation)
                    if speech.size:
                        pcm = _pcm16(speech)
                        segment.extend(pcm)
                        full.extend(pcm)

                seg_samples = len(segment) // 2

                if stopping:
                    # Best final: one full-context decode of the whole dictation.
                    final_text = ""
                    if 0 < len(full) // 2 <= max_final:
                        final_text = await self._decode(full)
                        await asyncio.to_thread(asr.release_cache)
                    if not final_text:  # too long (or empty) → stitch committed segments
                        await commit(final_segment=True)
                        final_text = self._texts()[2]
                    self._final_sent = True
                    await self._emit({"type": "final", "text": final_text}, generation)
                    audio_s = (len(full) // 2) / SAMPLE_RATE
                    wall_s = time.monotonic() - self.t_start
                    dropped = f", {self._dropped} frames dropped" if self._dropped else ""
                    _log(
                        f"dictation done: {self.packets} packets, "
                        f"{_human_bytes(self.bytes_in)}, {audio_s:.1f}s audio, "
                        f"{len(self.committed)} segments, {wall_s:.1f}s wall{dropped}"
                    )
                    return

                # commit at a natural pause or when the segment gets too long
                if self.vad.saw_speech and self.vad.silence_ms >= SILENCE_MS and seg_samples >= min_seg:
                    await commit(final_segment=False)
                    await self._emit_partial(generation=generation)
                    continue
                if seg_samples >= max_seg:
                    await commit(final_segment=False)
                    await self._emit_partial(generation=generation)
                    continue

                # live partial for the active segment
                if seg_samples >= min_seg and seg_samples - samples_at_partial >= partial_step:
                    text = await self._decode(segment)
                    samples_at_partial = seg_samples
                    await self._emit_partial(text, generation)
        except asyncio.CancelledError:
            raise            # cancellation is abort()'s doing — let it propagate
        except Exception as e:
            # A decode blew up (CUDA OOM, a wedged context). Report it and mark
            # the dictation finished, so the queue stops filling and a later
            # `start` on this connection is not silently ignored.
            if generation == self.generation:
                self.running = False
                self._final_sent = True
            await self._emit({"type": "status", "state": "error", "detail": str(e)},
                             generation)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    wyoming_server = None
    if WYOMING_PORT:
        try:
            from server.wyoming import start_wyoming
            wyoming_server = await start_wyoming(WYOMING_HOST, WYOMING_PORT)
            print(f"[blurtd] Wyoming listener on tcp://{WYOMING_HOST}:{WYOMING_PORT} "
                  f"(Home Assistant STT — no auth/TLS on this port)", flush=True)
        except Exception as e:
            # An optional side listener must never take down dictation. Port
            # 10300 is the shared Wyoming ASR convention, so a conflict with an
            # existing wyoming-* service is the likely cause.
            print(f"[blurtd] warn: Wyoming listener disabled — could not bind "
                  f"{WYOMING_HOST}:{WYOMING_PORT} ({e})", flush=True)
            wyoming_server = None
    yield
    if wyoming_server is not None:
        wyoming_server.close()
        try:
            # Python 3.12's wait_closed() also waits for live connections, and
            # close() does not terminate them — a peer holding an idle
            # connection (Home Assistant does) would hang shutdown outright.
            await asyncio.wait_for(wyoming_server.wait_closed(), timeout=5)
        except (asyncio.TimeoutError, Exception):
            pass


app = FastAPI(title="Blurt — Parakeet dictation server", lifespan=_lifespan)

# OpenAI-compatible transcription API (POST /v1/audio/transcriptions) — lets
# OpenAI SDKs and tools use Blurt as a drop-in base_url. See server/openai_api.py.
# Imported here rather than at the top because openai_api reads config back out
# of this module; it only ever does so from inside functions, so there is no
# cycle, but keep it that way.
from server.openai_api import router as openai_router, limit_upload_size  # noqa: E402
app.include_router(openai_router)
app.middleware("http")(limit_upload_size)


@app.get("/")
async def index():
    return FileResponse(HERE.parent / "static" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


async def send_json(ws: WebSocket, payload: dict):
    try:
        await ws.send_text(json.dumps(payload))
    except Exception:
        pass


def token_ok(supplied: str | None) -> bool:
    """Constant-time check of a client-supplied token against AUTH_TOKEN."""
    if not AUTH_TOKEN:
        return True
    return hmac.compare_digest(str(supplied or ""), AUTH_TOKEN)


def _start_converter(msg: dict) -> PcmConverter:
    """Build the converter for a `start` message's declared audio format.

    `audio` is optional; absent or partial, it falls back to the canonical
    16 kHz mono PCM16. Raises UnsupportedFormat for anything unusable,
    including an `audio` value that isn't an object at all.
    """
    audio = msg.get("audio")
    if audio is None:
        audio = {}
    if not isinstance(audio, dict):
        raise UnsupportedFormat("`audio` must be an object with rate/width/channels")
    try:
        rate = int(audio.get("rate", SAMPLE_RATE))
        width = int(audio.get("width", 2))
        channels = int(audio.get("channels", 1))
    except (TypeError, ValueError):
        raise UnsupportedFormat("`audio` rate/width/channels must be numbers")
    return PcmConverter(rate=rate, width=width, channels=channels)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=1008)
        return
    await ws.accept()
    await send_json(ws, server_info())

    async def emit(payload: dict):
        await send_json(ws, payload)

    sess = Session(emit)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                sess.add_audio(msg["bytes"])
            elif msg.get("text") is not None:
                try:
                    obj = json.loads(msg["text"])
                except ValueError:
                    continue          # not JSON — ignore, don't drop the socket
                if not isinstance(obj, dict):
                    continue          # valid JSON but not a message object
                cmd = obj.get("type")
                if cmd == "start":
                    try:
                        converter = _start_converter(obj)
                    except (UnsupportedFormat, ValueError, TypeError) as e:
                        await send_json(ws, {"type": "status", "state": "error",
                                             "detail": str(e), "id": obj.get("id")})
                        continue
                    await sess.start(obj.get("id"), converter)
                elif cmd == "stop":
                    # A stale stop (from a dictation this session already left
                    # behind) must not finalize the current one.
                    if obj.get("id") in (None, sess.dictation_id):
                        await sess.stop()
                elif cmd == "describe":
                    await send_json(ws, server_info())
                # unknown types: ignored (forward compatibility)
    except WebSocketDisconnect:
        pass
    finally:
        # The client is gone: drop the dictation rather than spending GPU time
        # on a final nobody will receive.
        sess.abort()
