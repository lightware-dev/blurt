"""
Protocol regression tests for blurtd — no GPU, no model download required.

Stubs the ASR engine and the Silero VAD, then drives the real server over real
sockets: the native WebSocket protocol, the Wyoming listener, and the
OpenAI-compatible REST API.

    python scripts/test_protocol.py            # all suites
    python scripts/test_protocol.py native     # pcm|native|wyoming|interop|openai

Install the test dependencies with `pip install -r requirements-test.txt` —
notably *not* torch or NeMo, since the model is stubbed. Suites whose optional
dependency is missing skip themselves rather than fail.

Deliberately covers the malformed-input and lifecycle cases, not just the happy
path: every check here that is named "survives" or "recovers" corresponds to a
bug that once took down a connection or wedged a session.
"""

from __future__ import annotations

import io
import sys
import json
import wave
import uuid
import asyncio
import contextlib
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

SR = 16000
WS_PORT, WY_PORT, HTTP_PORT = 25879, 10399, 25879

FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = ""):
    print(("  pass  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail and not ok else ""))
    if not ok:
        FAILURES.append(name)


# ---- stubs ---------------------------------------------------------------

class FakeASR:
    """Stands in for a real engine: deterministic text, ~one word per 0.5 s.

    Carries the whole engine surface from server/engine.py, not just what the
    decode path touches: the Wyoming `info` message reports the model's
    description, attribution, languages and version off the engine, so a stub
    missing them fails that suite with an AttributeError rather than a diff.
    """

    engine = "fake"
    model_name = "fake/parakeet"
    description = "Fake ASR (test stub)"
    attribution = {"name": "Blurt", "url": "https://blurtvoice.com"}
    languages = ["en"]
    model_version = "0"
    precision = "bf16"
    fail = False

    @property
    def is_loaded(self) -> bool:
        return True

    def transcribe(self, audio_f32) -> str:
        if self.fail:
            raise RuntimeError("CUDA out of memory (simulated)")
        if len(audio_f32) == 0:   # matches ParakeetASR: nothing in, nothing out
            return ""
        n = max(1, int(len(audio_f32) / SR / 0.5))
        return " ".join(f"w{i}" for i in range(n))

    def release_cache(self):
        pass

    def load(self):
        return self


def _rigged_whisper(asr):
    """Give a WhisperASR stub internals so transcribe() runs without transformers.

    load() returns early once a model is set, so filling in the three attributes
    it would have populated is enough to exercise the real decode path — the
    branch on audio length, the arguments each branch builds, and the text
    extraction — with nothing downloaded and no GPU.
    """
    import contextlib as _ctx

    proc_calls, gen_calls = [], []

    class _Inputs(dict):
        def to(self, *a, **k):
            return self               # BatchFeature.to casts in place; we only need the mapping

    class _Processor:
        def __call__(self, audio, **kw):
            proc_calls.append(kw)
            return _Inputs(input_features="F")

        def batch_decode(self, ids, skip_special_tokens=True):
            return ["  hello world  "]   # leading/trailing space is what Whisper emits

    class _Model:
        def generate(self, **kw):
            gen_calls.append(kw)
            return ["ids"]

    class _Torch:
        @staticmethod
        @_ctx.contextmanager
        def inference_mode():
            yield

    asr._model, asr._processor, asr._torch, asr.dtype = _Model(), _Processor(), _Torch(), "dt"
    asr.proc_calls, asr.gen_calls = proc_calls, gen_calls
    return asr


class FakeVAD:
    """Speech when a frame is loud; same gate + run-length interface as SileroVAD.

    Classifies whole frames rather than 512-sample windows, but reuses the real
    SpeechGate, so the padding behaviour under test is production code.
    """

    def __init__(self, threshold: float = 0.5,
                 preroll_ms: float = 250.0, hangover_ms: float = 200.0):
        from server.vad import SpeechGate
        self.gate = SpeechGate(preroll_ms, hangover_ms)
        self.reset()

    def reset(self):
        self.speech_run = 0
        self.silence_run = 0
        self.saw_speech = False
        self.gate.reset()

    def process(self, frame_f32):
        if len(frame_f32) == 0:
            return np.zeros(0, dtype=np.float32)
        speech = float(np.abs(frame_f32).mean()) > 0.01
        if speech:
            self.saw_speech = True
            self.speech_run += len(frame_f32)
            self.silence_run = 0
        else:
            self.silence_run += len(frame_f32)
            self.speech_run = 0
        keep = self.gate.push(frame_f32, speech)
        return np.concatenate(keep) if keep else np.zeros(0, dtype=np.float32)

    def flush(self):
        return np.zeros(0, dtype=np.float32)

    @property
    def silence_ms(self) -> float:
        return 1000.0 * self.silence_run / SR


def install_stubs():
    """Swap in the stubs and shorten the timings so tests run in seconds."""
    import server.vad as vad_mod
    vad_mod.SileroVAD = FakeVAD

    import server.app as app
    app.asr = FakeASR()
    app.WYOMING_PORT = 0            # started explicitly by the wyoming suite
    app.SILENCE_MS = 200
    app.PARTIAL_INTERVAL_MS = 80
    app.MIN_SEGMENT_S = 0.05
    app.VAD_OFF_MS = 100
    return app


def pcm(seconds: float, loud: bool = True, rate: int = SR) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 440 * t) * (0.3 if loud else 0.0) * 32767).astype(np.int16).tobytes()


def wav_bytes(spans) -> bytes:
    """spans: [(seconds, loud), ...] -> a mono 16 kHz wav."""
    audio = b"".join(pcm(s, loud) for s, loud in spans)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(audio)
    return buf.getvalue()


@contextlib.asynccontextmanager
async def running_server(app, port: int):
    import uvicorn
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.02)
        yield server
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await asyncio.wait_for(task, timeout=10)


# ---- native WebSocket protocol ------------------------------------------

async def suite_native(app):
    import websockets
    url = f"ws://127.0.0.1:{WS_PORT}/ws"

    async def drain(ws, until="final", timeout=15):
        """Collect messages until `until` arrives; returns the list."""
        out = []
        while True:
            m = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            out.append(m)
            if m["type"] == until:
                return out

    async with running_server(app.app, WS_PORT):
        # --- handshake, structured partials, vad, id echo
        async with websockets.connect(url) as ws:
            info = json.loads(await ws.recv())
            check("info on connect", info["type"] == "info" and info["protocol"] == 1
                  and info["state"] == "ready" and info["audio"]["rate"] == SR)
            # The clients bound their wait for a `final` on this, so a server
            # that stops advertising it silently reverts them to guessing.
            check("info advertises the stop budget",
                  isinstance(info.get("stop_timeout_s"), (int, float))
                  and info["stop_timeout_s"] > 0)

            await ws.send(json.dumps({"type": "describe"}))
            check("describe -> info", json.loads(await ws.recv())["type"] == "info")

            did = uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": did,
                                      "audio": {"rate": SR, "width": 2, "channels": 1}}))
            ack = json.loads(await ws.recv())
            check("start ack carries id", ack["type"] == "status" and ack["id"] == did)

            for _ in range(12):
                await ws.send(pcm(0.1))
                await asyncio.sleep(0.02)
            for _ in range(5):
                await ws.send(pcm(0.1, loud=False))
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.5)
            await ws.send(json.dumps({"type": "stop", "id": did}))
            msgs = await drain(ws)

            vads = [m for m in msgs if m["type"] == "vad"]
            partials = [m for m in msgs if m["type"] == "partial"]
            final = msgs[-1]
            check("vad speech on/off", any(v["speech"] for v in vads)
                  and any(not v["speech"] for v in vads))
            check("partials are structured", bool(partials) and all(
                "committed" in p and "live" in p for p in partials))
            check("partial.text == committed + live", all(
                p["text"] == (p["committed"] + " " + p["live"]).strip() for p in partials))
            check("every event carries the id", all(m.get("id") == did for m in msgs))
            check("final has text", bool(final["text"]))

        # --- malformed input must not drop the connection
        async with websockets.connect(url) as ws:
            await ws.recv()
            for bad in ["5", '"hello"', "[1,2]", "not json at all", "{}"]:
                await ws.send(bad)
            await ws.send(json.dumps({"type": "unknown-future-type", "x": 1}))
            await ws.send(json.dumps({"type": "describe"}))
            got = json.loads(await asyncio.wait_for(ws.recv(), 5))
            check("survives malformed text frames", got["type"] == "info")

        # --- a bad audio declaration is an error, not a dropped connection
        async with websockets.connect(url) as ws:
            await ws.recv()
            for bad_audio in ["x", 5, {"rate": "fast"}, {"width": 3}, {"rate": 1}]:
                await ws.send(json.dumps({"type": "start", "id": "bad", "audio": bad_audio}))
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if not (m["type"] == "status" and m["state"] == "error"):
                    break
            check("bad audio declaration -> status error", m["type"] == "status"
                  and m["state"] == "error", detail=json.dumps(m))
            await ws.send(json.dumps({"type": "describe"}))
            check("survives bad audio declaration",
                  json.loads(await asyncio.wait_for(ws.recv(), 5))["type"] == "info")

        # --- odd-length binary frames on a declared non-native format
        async with websockets.connect(url) as ws:
            await ws.recv()
            did = uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": did,
                                      "audio": {"rate": 48000, "width": 2, "channels": 1}}))
            await ws.recv()
            await ws.send(b"\x01\x02\x03")           # not a whole sample
            await ws.send(pcm(0.4, rate=48000)[:-1])  # odd tail
            await ws.send(json.dumps({"type": "stop", "id": did}))
            msgs = await drain(ws)
            check("survives odd-length audio frames", msgs[-1]["type"] == "final")

        # --- a second start supersedes the first instead of wedging
        async with websockets.connect(url) as ws:
            await ws.recv()
            a, b = uuid.uuid4().hex, uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": a}))
            await ws.recv()
            for _ in range(4):
                await ws.send(pcm(0.1))
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"type": "start", "id": b}))
            ack = None
            for _ in range(20):
                m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                if m["type"] == "status" and m.get("id") == b:
                    ack = m
                    break
            check("second start is acked under its own id", ack is not None)
            for _ in range(8):
                await ws.send(pcm(0.1))
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"type": "stop", "id": b}))
            msgs = await drain(ws)
            check("second dictation finalizes", msgs[-1]["type"] == "final")
            check("no events under the superseded id",
                  not any(m.get("id") == a for m in msgs))

        # --- stale stop is ignored; the live dictation still finalizes
        async with websockets.connect(url) as ws:
            await ws.recv()
            did = uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": did}))
            await ws.recv()
            for _ in range(6):
                await ws.send(pcm(0.1))
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"type": "stop", "id": "some-other-dictation"}))
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"type": "stop", "id": did}))
            msgs = await drain(ws)
            check("stale stop ignored, real stop finalizes",
                  msgs[-1]["type"] == "final" and msgs[-1]["id"] == did)

        # --- a decode failure reports an error and the session recovers
        app.asr.fail = True
        async with websockets.connect(url) as ws:
            await ws.recv()
            did = uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": did}))
            await ws.recv()
            for _ in range(8):
                await ws.send(pcm(0.1))
                await asyncio.sleep(0.05)
            err = None
            with contextlib.suppress(Exception):
                for _ in range(20):
                    m = json.loads(await asyncio.wait_for(ws.recv(), 5))
                    if m["type"] == "status" and m.get("state") == "error":
                        err = m
                        break
            check("decode failure reports status error", err is not None)

            app.asr.fail = False
            did2 = uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": did2}))
            ack = json.loads(await asyncio.wait_for(ws.recv(), 5))
            check("session recovers after a decode failure",
                  ack["type"] == "status" and ack.get("id") == did2)
            for _ in range(6):
                await ws.send(pcm(0.1))
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"type": "stop", "id": did2}))
            msgs = await drain(ws)
            check("dictation after a failure still finalizes", msgs[-1]["type"] == "final")

        # --- stop always produces exactly one final, even with no audio at all
        async with websockets.connect(url) as ws:
            await ws.recv()
            did = uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": did}))
            await ws.recv()
            await ws.send(json.dumps({"type": "stop", "id": did}))
            msgs = await drain(ws)
            check("empty dictation still finalizes",
                  sum(1 for m in msgs if m["type"] == "final") == 1)

        # --- a minimal client: omits the optional id and audio declaration
        async with websockets.connect(url) as ws:
            await ws.recv()
            await ws.send(json.dumps({"type": "start"}))
            await ws.recv()
            for _ in range(6):
                await ws.send(pcm(0.1))
                await asyncio.sleep(0.02)
            await ws.send(json.dumps({"type": "stop"}))
            msgs = await drain(ws)
            check("minimal client (no id, no format) works", msgs[-1]["type"] == "final")


# ---- Wyoming -------------------------------------------------------------

async def suite_wyoming(app):
    from server.wyoming import start_wyoming, _encode, _read_event

    server = await start_wyoming("127.0.0.1", WY_PORT)
    try:
        async def connect():
            return await asyncio.open_connection("127.0.0.1", WY_PORT)

        async def send(w, etype, data=None, payload=b""):
            w.write(_encode(etype, data, payload))
            await w.drain()

        fmt = {"rate": SR, "width": 2, "channels": 1}

        # --- describe/info
        r, w = await connect()
        await send(w, "describe")
        etype, data, _ = await asyncio.wait_for(_read_event(r), 10)
        check("wyoming info", etype == "info" and data["asr"][0]["name"] == "blurt"
              and data["asr"][0]["supports_transcript_streaming"] is True)
        w.close()

        # --- a full transcription round trip
        r, w = await connect()
        await send(w, "transcribe", {"language": "en"})
        await send(w, "audio-start", fmt)
        for _ in range(12):
            await send(w, "audio-chunk", fmt, pcm(0.1))
            await asyncio.sleep(0.02)
        for _ in range(5):
            await send(w, "audio-chunk", fmt, pcm(0.1, loud=False))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        await send(w, "audio-stop")
        seen = []
        while True:
            etype, data, _ = await asyncio.wait_for(_read_event(r), 15)
            seen.append((etype, data))
            if etype == "transcript-stop":
                break
        types = [t for t, _ in seen]
        check("wyoming voice-started", "voice-started" in types)
        check("wyoming stream order", types.index("transcript-start")
              < types.index("transcript") < types.index("transcript-stop"))
        check("wyoming transcript text", bool(dict(seen)["transcript"]["text"]))

        # --- restarting a stream must not duplicate committed text
        await send(w, "audio-start", fmt)
        for _ in range(10):
            await send(w, "audio-chunk", fmt, pcm(0.1))
            await asyncio.sleep(0.02)
        for _ in range(5):
            await send(w, "audio-chunk", fmt, pcm(0.1, loud=False))
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.5)
        await send(w, "audio-start", fmt)          # restart without audio-stop
        for _ in range(8):
            await send(w, "audio-chunk", fmt, pcm(0.1))
            await asyncio.sleep(0.02)
        await send(w, "audio-stop")
        seen2 = []
        while True:
            etype, data, _ = await asyncio.wait_for(_read_event(r), 15)
            seen2.append((etype, data))
            if etype == "transcript-stop":
                break
        starts = [t for t, _ in seen2 if t == "transcript-start"]
        stops = [t for t, _ in seen2 if t == "transcript-stop"]
        chunks = [d["text"] for t, d in seen2 if t == "transcript-chunk"]
        check("restart keeps transcript-start/stop balanced",
              len(starts) == len(stops), detail=f"{len(starts)} starts, {len(stops)} stops")
        check("restart does not duplicate chunk text",
              len(chunks) == len(set(c.strip() for c in chunks)), detail=str(chunks))
        w.close()

        # --- malformed framing closes only that connection
        for label, raw in [
            ("scalar header", b"1\n"),
            ("array header", b"[1,2]\n"),
            ("non-json header", b"garbage\n"),
            ("string data_length", b'{"type":"x","data_length":"5"}\n12345'),
            ("negative payload_length", b'{"type":"x","payload_length":-1}\n'),
            ("huge data_length", b'{"type":"x","data_length":999999999999}\n'),
            ("list data", b'{"type":"ping","data_length":7}\n[1,2,3]'),
        ]:
            r2, w2 = await connect()
            w2.write(raw)
            await w2.drain()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(r2.read(100), 3)
            w2.close()
            # the listener must still serve a well-formed peer afterwards
            r3, w3 = await connect()
            await send(w3, "describe")
            try:
                etype, _, _ = await asyncio.wait_for(_read_event(r3), 5)
                ok = etype == "info"
            except Exception:
                ok = False
            w3.close()
            check(f"wyoming survives {label}", ok)

        # --- a chunk that omits the format keeps the declared one
        r, w = await connect()
        await send(w, "audio-start", {"rate": 48000, "width": 2, "channels": 1})
        for _ in range(10):
            await send(w, "audio-chunk", None, pcm(0.1, rate=48000))  # no format fields
            await asyncio.sleep(0.02)
        await send(w, "audio-stop")
        got = None
        while True:
            etype, data, _ = await asyncio.wait_for(_read_event(r), 15)
            if etype == "transcript":
                got = data["text"]
            if etype == "transcript-stop":
                break
        # 1 s of 48 kHz audio is 1 s of speech; the stub yields ~2 words. If the
        # converter had reverted to passthrough it would be 3x too long.
        check("wyoming keeps the declared rate for format-less chunks",
              got is not None and len(got.split()) <= 4, detail=repr(got))
        w.close()
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=5)


# ---- OpenAI-compatible API ----------------------------------------------

async def raw_upload(port: int, body: bytes, chunked: bool = False,
                     declared: int | None = None):
    """POST to /v1/audio/transcriptions over a raw socket, so the framing can
    lie in ways httpx won't. Returns (status_code_or_None, bytes_actually_sent).

    Reads concurrently with writing: the server answers (and may reset) long
    before a big body finishes going out, and a write-then-read would deadlock
    or lose the response.
    """
    boundary = "----blurttest"
    part = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
            f"filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n").encode()
    framing = ("Transfer-Encoding: chunked" if chunked
               else f"Content-Length: {len(part) + len(body) if declared is None else declared}")
    head = (f"POST /v1/audio/transcriptions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
            f"{framing}\r\n\r\n").encode()

    if chunked:
        payload = [b"%x\r\n" % len(part) + part + b"\r\n"]
        for off in range(0, len(body), 65536):
            piece = body[off:off + 65536]
            payload.append(b"%x\r\n" % len(piece) + piece + b"\r\n")
        payload.append(b"0\r\n\r\n")
    else:
        payload = [part] + [body[off:off + 65536] for off in range(0, len(body), 65536)]

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    resp = b""
    sent = 0

    async def read_resp():
        nonlocal resp
        with contextlib.suppress(Exception):
            resp = await reader.read(400)

    rt = asyncio.create_task(read_resp())
    try:
        writer.write(head)
        await writer.drain()
        for piece in payload:
            writer.write(piece)
            await writer.drain()
            sent += len(piece)
    except Exception:
        pass  # the server resetting mid-upload is one of the outcomes under test
    with contextlib.suppress(Exception):
        await asyncio.wait_for(rt, timeout=15)
    rt.cancel()
    with contextlib.suppress(Exception):
        writer.close()

    status = None
    if resp.startswith(b"HTTP/1.1 "):
        with contextlib.suppress(ValueError):
            status = int(resp[9:12])
    return status, sent


async def suite_openai(app):
    try:
        import httpx
    except ImportError:
        print("  skip  openai suite (httpx not installed)")
        return

    wav = wav_bytes([(1.2, True), (0.5, False), (0.8, True)])

    async with running_server(app.app, HTTP_PORT):
        base = f"http://127.0.0.1:{HTTP_PORT}"
        async with httpx.AsyncClient(timeout=30) as c:
            files = {"file": ("a.wav", wav, "audio/wav")}

            r = await c.get(f"{base}/v1/models")
            ids = [m["id"] for m in r.json()["data"]]
            check("models list", r.status_code == 200 and "whisper-1" in ids)

            r = await c.post(f"{base}/v1/audio/transcriptions", files=files,
                             data={"model": "whisper-1"})
            check("json format", r.status_code == 200 and bool(r.json().get("text")))

            r = await c.post(f"{base}/v1/audio/transcriptions", files=files,
                             data={"response_format": "text"})
            check("text format", r.status_code == 200 and bool(r.text.strip()))

            r = await c.post(f"{base}/v1/audio/transcriptions", files=files,
                             data={"response_format": "verbose_json"})
            v = r.json()
            check("verbose_json segments", r.status_code == 200 and len(v["segments"]) >= 2
                  and all(s["end"] >= s["start"] for s in v["segments"]))

            r = await c.post(f"{base}/v1/audio/transcriptions", files=files,
                             data={"response_format": "srt"})
            check("srt format", r.status_code == 200 and "-->" in r.text)

            r = await c.post(f"{base}/v1/audio/transcriptions", files=files,
                             data={"response_format": "vtt"})
            check("vtt format", r.status_code == 200 and r.text.startswith("WEBVTT"))

            r = await c.post(f"{base}/v1/audio/transcriptions", files=files,
                             data={"response_format": "nope"})
            check("unknown response_format -> 400", r.status_code == 400)

            r = await c.post(f"{base}/v1/audio/transcriptions",
                             files={"file": ("x.bin", b"not audio", "application/octet-stream")})
            check("undecodable upload -> 400", r.status_code == 400
                  and "error" in r.json())
            # ffmpeg's stderr is folded into the ValueError; it stays in the
            # log, so the caller gets this exact message and nothing else.
            check("400 carries no decoder detail",
                  r.json()["error"]["message"]
                  == "Could not decode audio (unsupported or corrupt file).",
                  detail=r.text)

            # oversized upload rejected on Content-Length, before the body is read
            import server.openai_api as oa
            original_cap = oa.MAX_UPLOAD_MB
            oa.MAX_UPLOAD_MB = 1
            try:
                r = await c.post(f"{base}/v1/audio/transcriptions",
                                 files={"file": ("big.wav", b"\0" * (2 * 1024 * 1024),
                                                 "audio/wav")})
                check("oversized upload -> 413", r.status_code == 413,
                      detail=str(r.status_code))

                # A chunked body carries no Content-Length, so the size check
                # used to short-circuit and let an unauthenticated client
                # stream an unbounded upload to disk. httpx can't be made to
                # send one against a known length, so this goes over a raw
                # socket.
                status, _ = await raw_upload(HTTP_PORT, chunked=True,
                                             body=b"C" * (3 * 1024 * 1024))
                check("chunked upload -> 411", status == 411, detail=str(status))

                # A declared Content-Length is a claim — but it's a claim the
                # HTTP framing layer enforces, so a client that under-declares
                # can't spool more than it declared past the check above.
                status, sent = await raw_upload(HTTP_PORT, declared=1000,
                                                body=b"D" * (3 * 1024 * 1024))
                check("under-declared Content-Length is not honoured",
                      status is not None and status >= 400,
                      detail=f"status={status} sent={sent}")
            finally:
                oa.MAX_UPLOAD_MB = original_cap

            # streaming
            deltas, done, sentinel = [], None, False
            async with c.stream("POST", f"{base}/v1/audio/transcriptions", files=files,
                                data={"stream": "true"}) as r:
                async for line in r.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    body = line[6:]
                    if body == "[DONE]":
                        sentinel = True
                        break
                    ev = json.loads(body)
                    if ev["type"] == "transcript.text.delta":
                        deltas.append(ev["delta"])
                    elif ev["type"] == "transcript.text.done":
                        done = ev["text"]
            check("stream deltas + done", len(deltas) >= 2 and done == "".join(deltas))
            check("stream terminates with [DONE]", sentinel)

            # a decode failure mid-stream must still terminate the stream
            app.asr.fail = True
            events, sentinel = [], False
            with contextlib.suppress(Exception):
                async with c.stream("POST", f"{base}/v1/audio/transcriptions", files=files,
                                    data={"stream": "true"}) as r:
                    async for line in r.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        body = line[6:]
                        if body == "[DONE]":
                            sentinel = True
                            break
                        events.append(json.loads(body))
            check("stream error is reported in-band",
                  any(e.get("type") == "error" for e in events), detail=str(events[:2]))
            check("stream still terminates on error", sentinel)
            # The exception text comes from NeMo/PyTorch and can name the
            # checkpoint path or the GPU; the caller gets a fixed message.
            check("stream error carries no exception detail",
                  not any("simulated" in json.dumps(e) for e in events),
                  detail=str(events[:2]))

            r = await c.post(f"{base}/v1/audio/transcriptions", files=files)
            check("non-stream decode failure -> 500 in OpenAI shape",
                  r.status_code == 500 and "error" in r.json())
            check("500 carries no exception detail", "simulated" not in r.text,
                  detail=r.text)
            app.asr.fail = False

            # auth
            app.AUTH_TOKEN = "sekrit"
            r = await c.post(f"{base}/v1/audio/transcriptions", files=files)
            check("401 without key", r.status_code == 401)
            r = await c.post(f"{base}/v1/audio/transcriptions", files=files,
                             headers={"Authorization": "Bearer sekrit"})
            check("200 with bearer key", r.status_code == 200)
            app.AUTH_TOKEN = ""


# ---- PCM conversion ------------------------------------------------------

async def suite_pcm(app):
    from server.pcm import PcmConverter, UnsupportedFormat

    check("passthrough is byte-identical",
          PcmConverter().convert(pcm(0.1)) == pcm(0.1))

    # chunk-size independence: the resampler must interpolate across frames
    for rate in (8000, 44100, 48000):
        sig = pcm(0.5, rate=rate)
        whole = PcmConverter(rate, 2, 1).convert(sig)
        chunked = b""
        conv = PcmConverter(rate, 2, 1)
        for i in range(0, len(sig), 74):        # deliberately odd chunk size
            chunked += conv.convert(sig[i:i + 74])
        a = np.frombuffer(whole, dtype=np.int16).astype(float)
        b = np.frombuffer(chunked, dtype=np.int16).astype(float)
        n = min(len(a), len(b))
        check(f"resample {rate}->16k is chunk-independent",
              len(a) == len(b) and n > 0 and np.abs(a[:n] - b[:n]).max() == 0,
              detail=f"{len(a)} vs {len(b)}")

    # odd byte counts are carried, never raised
    conv = PcmConverter(48000, 2, 1)
    try:
        conv.convert(b"\x01\x02\x03")
        conv.convert(b"\x04")
        check("odd-length frames are carried", True)
    except Exception as e:
        check("odd-length frames are carried", False, detail=str(e))

    # stereo stays aligned even when split on odd boundaries
    mono = np.frombuffer(pcm(0.1), dtype=np.int16)
    stereo = np.repeat(mono, 2).astype(np.int16).tobytes()
    conv = PcmConverter(SR, 2, 2)
    out = b"".join(conv.convert(stereo[i:i + 7]) for i in range(0, len(stereo), 7))
    got = np.frombuffer(out, dtype=np.int16)
    check("stereo downmix stays channel-aligned",
          np.array_equal(got, mono[:len(got)]) and len(got) > 0)

    for bad in ((SR, 3, 1), (SR, 2, 7), (1, 2, 1), (999999, 2, 1)):
        try:
            PcmConverter(*bad)
            check(f"rejects {bad}", False)
        except UnsupportedFormat:
            check(f"rejects {bad}", True)


# ---- speech gating -------------------------------------------------------

async def suite_gate(app):
    """The gate that keeps room noise away from the model.

    Parakeet transcribes whatever it is handed, so background chatter the VAD
    scores as non-speech must never be buffered — while the padding that stops
    the gate from clipping word edges must still let it through.
    """
    from server.vad import SpeechGate

    chunk = np.ones(1600, dtype=np.float32)      # 100 ms
    g = SpeechGate(preroll_ms=250.0, hangover_ms=200.0)

    check("non-speech is dropped", g.push(chunk, False) == [] and not g.open)

    # onset: everything held as pre-roll comes back out ahead of the speech
    for _ in range(4):
        g.push(chunk, False)
    onset = g.push(chunk, True)
    check("onset flushes pre-roll ahead of speech",
          len(onset) > 1 and onset[-1] is chunk, detail=f"{len(onset)} chunks")

    # ...but only preroll_ms of it, however long the silence ran
    g2 = SpeechGate(preroll_ms=250.0, hangover_ms=200.0)
    for _ in range(200):                          # 20 s of silence
        g2.push(chunk, False)
    kept = sum(len(c) for c in g2.push(chunk, True)) - len(chunk)
    check("pre-roll is bounded", kept <= int(0.25 * SR) + len(chunk),
          detail=f"{kept / SR:.2f}s")

    # hangover keeps the tail of a word, then closes
    g3 = SpeechGate(preroll_ms=250.0, hangover_ms=200.0)
    g3.push(chunk, True)
    after = [g3.push(chunk, False) for _ in range(4)]
    check("hangover keeps the word's tail, then closes",
          after[0] and after[1] and not after[2] and not after[3] and not g3.open,
          detail=str([len(a) for a in after]))

    # a reset must not leave stale audio to be prepended to the next dictation
    g3.push(chunk, False)
    g3.reset()
    check("reset drops held pre-roll", g3.push(chunk, True) == [chunk])

    # end to end: a dictation of nothing but rejected audio yields no text
    import websockets
    url = f"ws://127.0.0.1:{WS_PORT}/ws"
    async with running_server(app.app, WS_PORT):
        async with websockets.connect(url) as ws:
            await ws.recv()
            did = uuid.uuid4().hex
            await ws.send(json.dumps({"type": "start", "id": did}))
            await ws.recv()
            for _ in range(12):
                await ws.send(pcm(0.1, loud=False))
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.3)
            await ws.send(json.dumps({"type": "stop", "id": did}))
            msgs = []
            while True:
                m = json.loads(await asyncio.wait_for(ws.recv(), 15))
                msgs.append(m)
                if m["type"] == "final":
                    break
            partials = [m for m in msgs if m["type"] == "partial"]
            check("non-speech never reaches the model",
                  not msgs[-1]["text"] and not any(p["text"] for p in partials),
                  detail=json.dumps(msgs[-1]))


# ---- Wyoming interop, driven by the official client library ---------------

async def suite_interop(app):
    """Same round trip as suite_wyoming, but driven by the real `wyoming`
    package — the library Home Assistant itself uses — rather than by our own
    framing code. Catches anything our encoder and its decoder disagree on.
    Skipped when the package isn't installed; CI installs it."""
    try:
        from wyoming.client import AsyncTcpClient
        from wyoming.info import Describe, Info
        from wyoming.asr import (Transcribe, Transcript, TranscriptStart,
                                 TranscriptChunk, TranscriptStop)
        from wyoming.audio import AudioStart, AudioChunk, AudioStop
    except ImportError:
        print("  skip  interop suite (pip install wyoming)")
        return

    from server.wyoming import start_wyoming

    server = await start_wyoming("127.0.0.1", WY_PORT)
    try:
        async with AsyncTcpClient("127.0.0.1", WY_PORT) as client:
            await client.write_event(Describe().event())
            ev = await asyncio.wait_for(client.read_event(), 10)
            check("official client parses info", ev is not None and Info.is_type(ev.type))
            program = Info.from_event(ev).asr[0]
            check("asr program advertises streaming",
                  program.name == "blurt" and program.installed
                  and getattr(program, "supports_transcript_streaming", False) is True)
            # The language list now comes off the loaded engine (Parakeet's 25,
            # Whisper's ~100, or one if WHISPER_LANGUAGE pins it), so this checks
            # the round trip against what the engine actually advertises.
            import server.app as app_mod
            check("model languages round-trip",
                  list(program.models[0].languages) == list(app_mod.asr.languages))

        async with AsyncTcpClient("127.0.0.1", WY_PORT) as client:
            await client.write_event(Transcribe(language="en").event())
            await client.write_event(AudioStart(rate=SR, width=2, channels=1).event())
            for _ in range(12):
                await client.write_event(
                    AudioChunk(rate=SR, width=2, channels=1, audio=pcm(0.1)).event())
                await asyncio.sleep(0.02)
            for _ in range(5):
                await client.write_event(
                    AudioChunk(rate=SR, width=2, channels=1,
                               audio=pcm(0.1, loud=False)).event())
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.5)
            await client.write_event(AudioStop().event())

            saw_start = saw_stop = False
            final = None
            while True:
                ev = await asyncio.wait_for(client.read_event(), 15)
                if ev is None:
                    break
                if TranscriptStart.is_type(ev.type):
                    saw_start = True
                elif TranscriptChunk.is_type(ev.type):
                    TranscriptChunk.from_event(ev)      # must parse
                elif Transcript.is_type(ev.type):
                    final = Transcript.from_event(ev).text
                elif TranscriptStop.is_type(ev.type):
                    saw_stop = True
                    break
            check("official client sees the full result stream",
                  saw_start and saw_stop and bool(final), detail=repr(final))
    finally:
        server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(server.wait_closed(), timeout=5)


# ---- precision selection -------------------------------------------------

async def suite_precision(_app):
    """PARAKEET_DTYPE picks the checkpoint; bf16 stays the default.

    Pure config, so it runs without a GPU: it guards the thing an fp16 build could
    plausibly break — a stray env var silently moving the default server off bf16,
    or the two precisions sharing one cache path and loading each other's weights.
    """
    import importlib.util  # .util for the torch probe below; also binds importlib
    import os

    import server.asr as asr

    saved = {k: os.environ.get(k) for k in
             ("PARAKEET_DTYPE", "PARAKEET_BF16_CKPT", "PARAKEET_FP16_CKPT",
              "PARAKEET_NVFP4_SNAPSHOT", "PARAKEET_REPO")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        asr = importlib.reload(asr)

        check("default precision is bf16", asr.ParakeetASR().precision == "bf16")
        check("aliases resolve", [asr.resolve_precision(x) for x in
                                  ("fp16", "float16", "half", "BF16", None)]
              == ["fp16", "fp16", "fp16", "bf16", "bf16"])
        check("nvfp4 aliases resolve", [asr.resolve_precision(x) for x in
                                        ("nvfp4", "fp4", "int4", "4bit", "NVFP4")]
              == ["nvfp4"] * 5)
        bad = False
        try:
            asr.resolve_precision("int3")
        except ValueError:
            bad = True
        check("unknown dtype rejected", bad)

        bf16, fp16 = asr.ParakeetASR("bf16"), asr.ParakeetASR("fp16")
        nvfp4 = asr.ParakeetASR("nvfp4")
        check("precisions use distinct checkpoints",
              len({bf16.ckpt_path(), fp16.ckpt_path(), nvfp4.ckpt_path()}) == 3)
        check("bf16 default path unchanged",
              bf16.ckpt_path().endswith("parakeet-tdt-0.6b-v3-bf16.nemo"))
        check("fp16 default path is the fp16 file",
              fp16.ckpt_path().endswith("parakeet-tdt-0.6b-v3-fp16.nemo"))
        # A directory, not a .nemo — the snapshot is several files.
        check("nvfp4 default path is the snapshot directory",
              nvfp4.ckpt_path().endswith("parakeet-tdt-0.6b-v3-nvfp4"))
        check("only nvfp4 loads from a snapshot",
              [asr.PRECISIONS[p]["kind"] for p in ("bf16", "fp16", "nvfp4")]
              == ["nemo", "nemo", "snapshot"])
        # nvfp4 is W4A16: four-bit weights, but everything computed stays bf16, so
        # it must not report itself as some exotic activation dtype.
        if importlib.util.find_spec("torch") is not None:
            import torch  # noqa: PLC0415  (guarded by the find_spec above)

            check("nvfp4 runs bf16 activations",
                  nvfp4.torch_dtype() is torch.bfloat16
                  and asr.ParakeetASR("fp16").torch_dtype() is torch.float16)

        os.environ["PARAKEET_FP16_CKPT"] = "/tmp/custom-fp16.nemo"
        check("PARAKEET_FP16_CKPT overrides only fp16",
              asr.ParakeetASR("fp16").ckpt_path() == "/tmp/custom-fp16.nemo"
              and asr.ParakeetASR("bf16").ckpt_path() != "/tmp/custom-fp16.nemo")
        os.environ.pop("PARAKEET_FP16_CKPT")

        os.environ["PARAKEET_NVFP4_SNAPSHOT"] = "/tmp/custom-nvfp4"
        check("PARAKEET_NVFP4_SNAPSHOT overrides only nvfp4",
              asr.ParakeetASR("nvfp4").ckpt_path() == "/tmp/custom-nvfp4"
              and asr.ParakeetASR("bf16").ckpt_path() != "/tmp/custom-nvfp4")
        os.environ.pop("PARAKEET_NVFP4_SNAPSHOT")

        os.environ["PARAKEET_DTYPE"] = "fp16"
        check("PARAKEET_DTYPE switches the engine", asr.ParakeetASR().precision == "fp16")
        check("explicit argument beats the env var",
              asr.ParakeetASR("bf16").precision == "bf16")

        # With no cache and no download, load() must say "build one" rather than
        # leaving you to guess. Off a GPU the loader legitimately stops one step
        # earlier, at "no CUDA device". Needs torch — the rest of this suite is
        # deliberately torch-free, so CI without it skips just this check.
        if importlib.util.find_spec("torch") is None:
            print("  skip  missing fp16 checkpoint message (torch not installed)")
        else:
            import torch  # noqa: PLC0415  (guarded by the find_spec above)

            # The repo has to be a nonexistent name, or this check is vacuous: with
            # a working network the loader simply *downloads* the checkpoint it was
            # told is missing, loads it, and raises nothing. It only looked green
            # because CI has no torch and skips the whole block.
            os.environ["PARAKEET_REPO"] = "lightware-dev/no-such-repo-for-tests"
            os.environ["PARAKEET_FP16_CKPT"] = "/nonexistent/nope.nemo"
            asr = importlib.reload(asr)
            err = ""
            try:
                asr.ParakeetASR("fp16").load()
            except Exception as e:
                err = str(e)
            want = ("build_bf16_ckpt.py --dtype fp16" if torch.cuda.is_available()
                    else "no CUDA device was found")
            check("missing fp16 checkpoint fails with the actionable message",
                  want in err, detail=err[:160])

            # Same for nvfp4, which must name its own builder rather than
            # build_bf16_ckpt.py — that script cannot produce a snapshot.
            os.environ.pop("PARAKEET_FP16_CKPT", None)
            os.environ["PARAKEET_NVFP4_SNAPSHOT"] = "/nonexistent/snapshot-dir"
            asr = importlib.reload(asr)
            err = ""
            try:
                asr.ParakeetASR("nvfp4").load()
            except Exception as e:
                err = str(e)
            want = ("build_nvfp4_snapshot.py" if torch.cuda.is_available()
                    else "no bf16-capable CUDA device was found")
            check("missing nvfp4 snapshot fails with the actionable message",
                  want in err, detail=err[:160])
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(asr)


# ---- runner --------------------------------------------------------------

# ---- engine selection ----------------------------------------------------

async def suite_engine(_app):
    """BLURT_ASR_ENGINE picks the engine; Parakeet stays the default.

    Pure config, like the precision suite, so it runs with neither a GPU nor
    torch: it guards the things a second engine can plausibly break — a stray
    env var moving the default server off Parakeet, the two engines drifting out
    of interface parity behind the single `asr` global, or Whisper quietly
    accepting a Parakeet-only precision and loading something nobody asked for.
    """
    import importlib.util   # .util for the torch probe below; also binds importlib
    import os

    import server.engine as engine
    import server.asr as parakeet
    import server.whisper as whisper

    saved = {k: os.environ.get(k) for k in
             ("BLURT_ASR_ENGINE", "WHISPER_MODEL", "WHISPER_DTYPE",
              "WHISPER_LANGUAGE", "WHISPER_TASK", "PARAKEET_DTYPE")}
    try:
        for k in saved:
            os.environ.pop(k, None)
        engine = importlib.reload(engine)
        whisper = importlib.reload(whisper)

        check("default engine is parakeet", engine.resolve_engine(None) == "parakeet"
              and engine.create_asr().engine == "parakeet")
        check("engine aliases resolve",
              [engine.resolve_engine(x) for x in ("whisper", "OpenAI", "nemo", "Parakeet", "")]
              == ["whisper", "whisper", "parakeet", "parakeet", "parakeet"])
        bad = False
        try:
            engine.resolve_engine("wav2vec")
        except ValueError:
            bad = True
        check("unknown engine rejected", bad)

        check("create_asr builds the requested engine",
              engine.create_asr("whisper").engine == "whisper"
              and isinstance(engine.create_asr("whisper"), whisper.WhisperASR)
              and isinstance(engine.create_asr("parakeet"), parakeet.ParakeetASR))

        os.environ["BLURT_ASR_ENGINE"] = "whisper"
        check("BLURT_ASR_ENGINE switches the engine",
              engine.create_asr().engine == "whisper")
        check("explicit argument beats the env var",
              engine.create_asr("parakeet").engine == "parakeet")
        os.environ.pop("BLURT_ASR_ENGINE")

        # Interface parity: server/app.py holds one of these through a single
        # global and never learns which, so anything one engine grows and the
        # other doesn't is a runtime AttributeError in whichever listener reads it.
        surface = ("engine", "model_name", "description", "attribution", "languages",
                   "model_version", "precision", "dtype", "is_loaded", "load",
                   "transcribe", "release_cache", "torch_dtype")
        engines = [parakeet.ParakeetASR(), whisper.WhisperASR()]
        missing = {f"{e.engine}.{a}" for e in engines for a in surface if not hasattr(e, a)}
        check("engines expose the same surface", not missing, detail=str(sorted(missing)))
        check("neither engine starts out loaded",
              not any(e.is_loaded for e in engines))
        check("engines decline empty audio without loading a model",
              [e.transcribe(np.zeros(0, dtype=np.float32)) for e in engines] == ["", ""]
              and not any(e.is_loaded for e in engines))

        # ---- whisper configuration ----
        w = whisper.WhisperASR()
        check("whisper defaults to large-v3-turbo in bf16",
              w.model_name == "openai/whisper-large-v3-turbo" and w.precision == "bf16")
        check("whisper auto-detects language by default",
              w.language is None and w.task == "transcribe")
        check("whisper dtype aliases resolve",
              [whisper.resolve_precision(x) for x in ("fp16", "float16", "half", "BF16", None)]
              == ["fp16", "fp16", "fp16", "bf16", "bf16"])
        # 4-bit is a Parakeet-only path (it needs a calibrated snapshot); Whisper
        # must reject it rather than silently fall back to a half precision.
        rejected = 0
        for bad_dtype in ("nvfp4", "int4", "fp32"):
            try:
                whisper.resolve_precision(bad_dtype)
            except ValueError:
                rejected += 1
        check("whisper rejects precisions it does not have", rejected == 3)
        check("whisper language aliases mean auto",
              [whisper.resolve_language(x) for x in ("", "auto", "AUTO", None, " en ")]
              == [None, None, None, None, "en"])
        bad = False
        try:
            whisper.resolve_task("summarize")
        except ValueError:
            bad = True
        check("whisper rejects unknown tasks", bad)

        os.environ.update({"WHISPER_MODEL": "openai/whisper-small",
                           "WHISPER_DTYPE": "fp16",
                           "WHISPER_LANGUAGE": "pt",
                           "WHISPER_TASK": "translate"})
        w = whisper.WhisperASR()
        check("whisper env overrides apply",
              (w.model_name, w.precision, w.language, w.task)
              == ("openai/whisper-small", "fp16", "pt", "translate"))
        # A pinned language is a hard setting: audio in anything else comes back
        # wrong, so Home Assistant must not be told the server takes 99 languages.
        check("a pinned language is the only one advertised", w.languages == ["pt"])
        check("auto-detect advertises the full list",
              len(whisper.WhisperASR(language="auto").languages) > 90)
        check("whisper describes what is loaded",
              w.description == "OpenAI Whisper — openai/whisper-small (fp16, pt, translate)"
              and w.attribution["url"] == "https://huggingface.co/openai/whisper-small")
        # An English-only checkpoint takes neither a language nor a task token, so
        # the engine must not offer to serve 99 languages or promise translation.
        en_only = whisper.WhisperASR(model="openai/whisper-small.en", language="pt",
                                     task="translate")
        check("english-only checkpoints advertise english alone",
              en_only.multilingual is False and en_only.languages == ["en"]
              and "translate" not in en_only.description
              and whisper.WhisperASR(model="openai/whisper-small").multilingual is True)
        check("whisper model version comes off the checkpoint name",
              [whisper.WhisperASR(model=m).model_version for m in
               ("openai/whisper-large-v3-turbo", "openai/whisper-large-v2", "openai/whisper-small")]
              == ["3", "2", "1"])
        # Parakeet must be unmoved by any of the WHISPER_* vars above.
        check("whisper config does not touch parakeet",
              parakeet.ParakeetASR().precision == "bf16"
              and parakeet.ParakeetASR().model_name == "nvidia/parakeet-tdt-0.6b-v3")

        # ---- whisper decode plumbing ----
        for k in ("WHISPER_MODEL", "WHISPER_DTYPE", "WHISPER_LANGUAGE", "WHISPER_TASK"):
            os.environ.pop(k, None)   # back to defaults; the overrides above are done

        # Whisper's encoder sees a fixed 30 s window, so anything longer takes
        # transformers' sequential long-form path — which needs untruncated
        # features, an attention mask and timestamps, none of which the ordinary
        # call passes. Getting that branch wrong silently truncates every final
        # decode of a long dictation at 30 s, so it is checked here with a stubbed
        # model rather than left to a GPU no CI has.
        rigged = _rigged_whisper(whisper.WhisperASR())
        check("short audio decodes in one pass",
              rigged.transcribe(np.zeros(5 * SR, dtype=np.float32)) == "hello world"
              and rigged.proc_calls[0] == {"sampling_rate": SR, "return_tensors": "pt"}
              and rigged.gen_calls[0] == {"input_features": "F", "task": "transcribe"})

        rigged = _rigged_whisper(whisper.WhisperASR(language="pt"))
        rigged.transcribe(np.zeros(40 * SR, dtype=np.float32))
        check("long audio takes the long-form path",
              rigged.proc_calls[0] == {"sampling_rate": SR, "return_tensors": "pt",
                                       "truncation": False, "padding": "longest",
                                       "return_attention_mask": True}
              and rigged.gen_calls[0] == {"input_features": "F", "task": "transcribe",
                                          "language": "pt", "return_timestamps": True,
                                          "condition_on_prev_tokens": False})

        # An `.en` checkpoint raises on either argument rather than ignoring it.
        rigged = _rigged_whisper(whisper.WhisperASR(model="openai/whisper-small.en",
                                                    language="pt", task="translate"))
        rigged.transcribe(np.zeros(2 * SR, dtype=np.float32))
        check("english-only checkpoints get no language or task",
              rigged.gen_calls[0] == {"input_features": "F"})

        if importlib.util.find_spec("torch") is not None:
            import torch  # noqa: PLC0415  (guarded by the find_spec above)

            check("whisper dtypes map to torch",
                  whisper.WhisperASR(precision="fp16").torch_dtype() is torch.float16
                  and whisper.WhisperASR(precision="bf16").torch_dtype() is torch.bfloat16)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(whisper)
        importlib.reload(engine)


SUITES = {
    "precision": suite_precision,
    "engine": suite_engine,
    "pcm": suite_pcm,
    "gate": suite_gate,
    "native": suite_native,
    "wyoming": suite_wyoming,
    "interop": suite_interop,
    "openai": suite_openai,
}


async def main():
    app = install_stubs()
    wanted = sys.argv[1:] or list(SUITES)
    for name in wanted:
        if name not in SUITES:
            print(f"unknown suite {name!r}; choose from {', '.join(SUITES)}")
            return 2
        print(f"\n{name}:")
        await SUITES[name](app)

    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
