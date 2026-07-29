"""
OpenAI-compatible transcription API — POST /v1/audio/transcriptions on the
native (TLS-capable) port, so anything that speaks the OpenAI Audio API can use
Blurt as a drop-in `base_url` replacement: OpenAI SDKs, Open WebUI, subtitle
tools, and the rest of that ecosystem.

Endpoints:
  POST /v1/audio/transcriptions   multipart form: file, model?, language?,
                                  prompt?, response_format?, temperature?,
                                  stream?, timestamp_granularities[]?
  GET  /v1/models                 model list (the real model + a whisper-1
                                  alias, for clients that validate the picker)

Behavior notes:
  * `model`, `prompt`, `temperature`, `timestamp_granularities` are accepted
    and ignored — there is one model and it auto-detects language.
  * response_format: json (default), text, srt, vtt, verbose_json.
  * Segment timestamps (srt/vtt/verbose_json) come from Silero VAD
    segmentation: cuts land in real pauses, so cue boundaries are honest, but
    they include the surrounding silence — treat them as approximate.
  * `stream=true` answers with SSE `transcript.text.delta` events (one per
    VAD segment, decoded in order) and a final `transcript.text.done`.
  * Audio decoding: soundfile first (wav/flac/ogg), then an ffmpeg subprocess
    for everything else (mp3/m4a/webm/…; ffmpeg ships in the Docker image).
  * Auth: if AUTH_TOKEN is set, requires `Authorization: Bearer <AUTH_TOKEN>`
    (or ?token= like the WebSocket). Errors use the OpenAI error shape.
"""

from __future__ import annotations

import io
import os
import json
import asyncio
import traceback
import subprocess

import numpy as np
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

router = APIRouter()

SR = 16000

# Upload ceiling. Enforced by middleware on Content-Length *before* the body is
# read, because FastAPI resolves the UploadFile dependency — spooling the whole
# body to disk — before the handler (and therefore before the auth check) runs.
MAX_UPLOAD_MB = float(os.getenv("MAX_UPLOAD_MB", "200"))
# Ceiling on decoded audio. A heavily-compressed upload expands enormously as
# float32 (a 6 kbps stream is ~85x its wire size), so the byte cap alone does
# not bound memory.
MAX_AUDIO_S = float(os.getenv("MAX_AUDIO_S", "7200"))


# ---- errors (OpenAI shape) ----------------------------------------------

def _error(status: int, message: str, err_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(status_code=status,
                        content={"error": {"message": message, "type": err_type,
                                           "param": None, "code": None}})


def _log_exc(what: str) -> None:
    """Print the current exception server-side, where it is useful.

    Deliberately not gated on LOG_STATS: that flag silences routine
    per-dictation metadata, and a failed transcription is not routine — an
    operator running with stats off still needs to see a CUDA OOM or a missing
    checkpoint. It doesn't weaken the SECURITY.md promise either, since what is
    printed is the exception, never transcribed text.
    """
    print(f"[blurtd] error: {what}\n{traceback.format_exc()}", end="", flush=True)


def _check_auth(request: Request) -> JSONResponse | None:
    from server.app import AUTH_TOKEN, token_ok
    if not AUTH_TOKEN:
        return None
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") \
        else request.query_params.get("token", "")
    if not token_ok(token):
        return _error(401, "Invalid API key.", "invalid_api_key")
    return None


async def limit_upload_size(request: Request, call_next):
    """Reject oversized uploads before the body is read.

    Registered as middleware rather than checked in the handler: by the time a
    handler runs, Starlette has already consumed and spooled the entire
    multipart body, so an in-handler check would come far too late to prevent
    an unauthenticated client from filling the disk.
    """
    if request.url.path.startswith("/v1/audio/") and MAX_UPLOAD_MB > 0:
        declared = request.headers.get("content-length")
        if declared is None:
            # No Content-Length means a chunked body, and a chunked body has no
            # declared size to check — so this used to fall straight through and
            # spool an unbounded upload to disk, unauthenticated, which is the
            # exact thing this middleware exists to stop. Refuse it instead.
            #
            # Safe for the actual client population: the OpenAI SDKs and
            # `curl -F` both send Content-Length. A client that insists on
            # streaming has to buffer and declare a length.
            return _error(411, "Content-Length required on uploads "
                               "(chunked transfer encoding is not accepted here).")
        try:
            if int(declared) > MAX_UPLOAD_MB * 1024 * 1024:
                return _error(413, f"File too large (limit {MAX_UPLOAD_MB:.0f} MB).")
        except ValueError:
            return _error(400, "Invalid Content-Length.")
    return await call_next(request)


# ---- audio decoding ------------------------------------------------------

def _decode_audio(data: bytes) -> np.ndarray:
    """Uploaded file bytes -> 16 kHz mono float32. Raises ValueError if undecodable."""
    try:
        import soundfile as sf
        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        if sr != SR:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=SR)
        return np.ascontiguousarray(audio, dtype=np.float32)
    except Exception:
        pass  # not a libsndfile format — let ffmpeg have a go

    try:
        proc = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0",
             "-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1"],
            input=data, capture_output=True, timeout=120)
    except FileNotFoundError:
        raise ValueError("could not decode audio (and ffmpeg is not installed "
                         "for compressed formats)")
    except subprocess.TimeoutExpired:
        raise ValueError("audio decode timed out")
    if proc.returncode != 0 or not proc.stdout:
        detail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        why = f": {detail[-1]}" if detail else ""
        raise ValueError(f"could not decode audio (unsupported or corrupt file){why}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


# ---- VAD segmentation ----------------------------------------------------

def _segment_bounds(audio: np.ndarray) -> list[tuple[int, int]]:
    """Split the file into decode segments at silences (offline pass of the
    same Silero VAD + thresholds the live path uses). Segments cover the whole
    file back-to-back, so cue timestamps include the surrounding silence."""
    from server.app import SILENCE_MS, MAX_SEGMENT_S, MIN_SEGMENT_S, VAD_THRESHOLD
    from server.vad import SileroVAD

    # Threshold only — segments here cover the file back-to-back, so an upload
    # is never gated the way the live path is: a caller who hands us a file has
    # already decided it is worth transcribing.
    vad = SileroVAD(threshold=VAD_THRESHOLD)
    win = 512
    max_seg = int(MAX_SEGMENT_S * SR)
    min_seg = int(MIN_SEGMENT_S * SR)
    bounds: list[tuple[int, int]] = []
    start = 0
    pos = 0
    for off in range(0, len(audio), win):
        chunk = audio[off:off + win]
        vad.process(chunk)
        pos = off + len(chunk)
        seg_len = pos - start
        if seg_len >= max_seg or (
                vad.saw_speech and vad.silence_ms >= SILENCE_MS and seg_len >= min_seg):
            bounds.append((start, pos))
            start = pos
            vad.reset()
    if start < len(audio):
        bounds.append((start, len(audio)))
    return bounds


async def _decode_segments(audio: np.ndarray):
    """Yield (start_s, end_s, text) per VAD segment, skipping silent ones."""
    from server.app import asr
    # The VAD sweep is one Silero forward pass per 512-sample window over the
    # whole file — seconds of solid CPU for a long upload. On the event loop it
    # would freeze every live dictation, so it goes to a thread like the decode.
    bounds = await asyncio.to_thread(_segment_bounds, audio)
    try:
        for s, e in bounds:
            text = await asyncio.to_thread(asr.transcribe, audio[s:e])
            if text:
                yield s / SR, e / SR, text
    finally:
        # Runs even if the caller abandons the generator (client disconnect).
        await asyncio.to_thread(asr.release_cache)


# ---- subtitle formatting -------------------------------------------------

def _ts(seconds: float, sep: str) -> str:
    ms = int(round(seconds * 1000))
    h, rem = divmod(ms, 3_600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _srt(segments: list[tuple[float, float, str]]) -> str:
    return "\n".join(
        f"{i + 1}\n{_ts(s, ',')} --> {_ts(e, ',')}\n{text}\n"
        for i, (s, e, text) in enumerate(segments))


def _vtt(segments: list[tuple[float, float, str]]) -> str:
    return "WEBVTT\n\n" + "\n".join(
        f"{_ts(s, '.')} --> {_ts(e, '.')}\n{text}\n"
        for s, e, text in segments)


# ---- endpoints -----------------------------------------------------------

@router.get("/v1/models")
async def models(request: Request):
    if (denied := _check_auth(request)) is not None:
        return denied
    from server.app import asr
    entry = {"object": "model", "created": 0, "owned_by": "blurt"}
    return {"object": "list", "data": [
        {"id": asr.model_name, **entry},
        {"id": "whisper-1", **entry},   # alias so stock model pickers work
    ]}


@router.post("/v1/audio/transcriptions")
async def transcriptions(
    request: Request,
    file: UploadFile = File(...),
    model: str = Form(""),                    # accepted, ignored (one model)
    language: str = Form(""),                 # accepted, ignored (auto-detect)
    prompt: str = Form(""),                   # accepted, ignored
    response_format: str = Form("json"),
    temperature: float = Form(0.0),           # accepted, ignored
    stream: bool = Form(False),
):
    if (denied := _check_auth(request)) is not None:
        return denied
    if response_format not in ("json", "text", "srt", "vtt", "verbose_json"):
        return _error(400, f"Unsupported response_format: {response_format!r}")

    data = await file.read()
    if not data:
        return _error(400, "Empty file.")
    try:
        audio = await asyncio.to_thread(_decode_audio, data)
    except ValueError:
        # Also a fixed message, though this one is our own text rather than a
        # library's: the ffmpeg branch folds the last line of ffmpeg's stderr
        # into the ValueError, which is the sort of internal detail that has no
        # business crossing the wire. It is still in the log, where whoever
        # runs the box can read it.
        _log_exc("upload could not be decoded")
        return _error(400, "Could not decode audio (unsupported or corrupt file).")
    if len(audio) == 0:
        return _error(400, "File contains no audio.")
    duration = len(audio) / SR
    if MAX_AUDIO_S > 0 and duration > MAX_AUDIO_S:
        return _error(413, f"Audio too long ({duration / 60:.0f} min; "
                           f"limit {MAX_AUDIO_S / 60:.0f} min).")

    from server.app import FINAL_MAX_S, asr

    if stream:
        # SSE: one transcript.text.delta per VAD segment, then the done event.
        async def sse():
            parts: list[str] = []
            try:
                async for _, _, text in _decode_segments(audio):
                    delta = (" " if parts else "") + text
                    parts.append(text)
                    yield "data: " + json.dumps(
                        {"type": "transcript.text.delta", "delta": delta}) + "\n\n"
            except Exception:
                # The 200 and the headers went out before the first decode, so
                # there is no status code left to fail with. Say so in-band and
                # still terminate the stream — otherwise SDK parsers block
                # until the socket times out.
                #
                # Same fixed message as the non-stream path: the exception text
                # comes from NeMo/PyTorch and can carry checkpoint paths and
                # driver detail. It goes to the log instead.
                _log_exc("streaming transcription failed")
                yield "data: " + json.dumps(
                    {"type": "error",
                     "error": {"message": "Transcription failed.",
                               "type": "server_error"}}) + "\n\n"
            else:
                yield "data: " + json.dumps(
                    {"type": "transcript.text.done", "text": " ".join(parts)}) + "\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(sse(), media_type="text/event-stream", headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stops a reverse proxy from buffering the whole "stream".
            "X-Accel-Buffering": "no",
        })

    needs_segments = response_format in ("srt", "vtt", "verbose_json")
    try:
        if needs_segments or duration > FINAL_MAX_S:
            segments = [seg async for seg in _decode_segments(audio)]
            text = " ".join(t for _, _, t in segments)
        else:
            # short file + text-only output: one full-context decode (best quality)
            text = await asyncio.to_thread(asr.transcribe, audio)
            await asyncio.to_thread(asr.release_cache)
            segments = [(0.0, duration, text)] if text else []
    except Exception:
        # A decode failure (CUDA OOM, no bf16 GPU) is the server's fault, and
        # belongs in the OpenAI error shape rather than a bare 500 traceback.
        # The cause is logged, not returned: AUTH_TOKEN is empty by default and
        # the daemon binds 0.0.0.0, so on a stock install anyone who can reach
        # the port can trigger this — and the exception text comes from NeMo /
        # PyTorch, which happily names the checkpoint path under ~/.cache/blurt
        # and the GPU it failed on.
        _log_exc("transcription failed")
        return _error(500, "Transcription failed.", "server_error")

    if response_format == "text":
        return PlainTextResponse(text)
    if response_format == "srt":
        return PlainTextResponse(_srt(segments), media_type="text/plain")
    if response_format == "vtt":
        return PlainTextResponse(_vtt(segments), media_type="text/vtt")
    if response_format == "verbose_json":
        return {
            "task": "transcribe",
            "language": language or "auto",
            "duration": round(duration, 3),
            "text": text,
            "segments": [
                {
                    "id": i, "seek": 0,
                    "start": round(s, 3), "end": round(e, 3), "text": t,
                    # neutral values for fields subtitle tools expect to exist
                    "tokens": [], "temperature": 0.0, "avg_logprob": 0.0,
                    "compression_ratio": 1.0, "no_speech_prob": 0.0,
                }
                for i, (s, e, t) in enumerate(segments)
            ],
        }
    return {"text": text}
