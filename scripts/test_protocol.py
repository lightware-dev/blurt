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
    """Stands in for ParakeetASR: deterministic text, ~one word per 0.5 s."""

    model_name = "fake/parakeet"
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

    from server.wyoming import start_wyoming, LANGUAGES

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
            check("model languages round-trip",
                  len(program.models[0].languages) == len(LANGUAGES))

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


# ---- runner --------------------------------------------------------------

SUITES = {
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
