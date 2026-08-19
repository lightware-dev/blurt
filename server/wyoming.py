"""
Wyoming protocol listener — plain-TCP peer for the home-automation voice
ecosystem (Home Assistant, Rhasspy), so Blurt can serve as a drop-in STT
backend alongside its native WebSocket protocol.

Wire format (per the reference `wyoming` package): each event is a JSON header
line ending in "\\n" — {"type", "version", "data_length"?, "payload_length"?} —
followed by `data_length` bytes of JSON event data, then `payload_length` bytes
of binary payload (PCM for audio-chunk).

Supported events:
  describe            -> info (asr program + model, streaming advertised)
  transcribe          -> accepted (language noted; the model auto-detects)
  audio-start         -> begin a dictation
  audio-chunk         -> PCM payload, converted from the declared rate/width/channels
  audio-stop          -> finalize -> transcript events
  ping                -> pong

Emitted during/after audio:
  voice-started / voice-stopped   from the server-side Silero VAD
  transcript-start                before the first result event
  transcript-chunk                each committed segment, as it commits (streaming)
  transcript                      the authoritative final text (full re-decode)
  transcript-stop                 end of the result stream
  error                           on decode failure

The chunk stream is append-only (committed segments never change), which is
exactly what Wyoming's chunk semantics require; the final `transcript` comes
from Blurt's full-context re-decode and supersedes the chunks. Unknown incoming
event types are ignored, per the Wyoming convention.

No auth, no TLS — that is the Wyoming ecosystem's norm, and AUTH_TOKEN does not
cover this port. The listener is therefore off unless WYOMING_PORT is set
(10300 is the ecosystem's ASR convention); WYOMING_HOST narrows the bind.
"""

from __future__ import annotations

import json
import asyncio

WYOMING_VERSION = "1.6.0"  # protocol level we speak (streaming ASR events)

# Ceiling on a single declared data/payload section. Real audio-chunk payloads
# are a few KB; this is generous for those while bounding a hostile claim.
MAX_SECTION_BYTES = 16 * 1024 * 1024
# Drop a peer that opens a connection and then says nothing. Home Assistant
# holds idle connections open, so this is deliberately long.
IDLE_TIMEOUT_S = 600.0

# Languages of parakeet-tdt-0.6b-v3, for Home Assistant's language picker.
# ---- framing ------------------------------------------------------------

def _encode(etype: str, data: dict | None = None, payload: bytes = b"") -> bytes:
    header: dict = {"type": etype, "version": WYOMING_VERSION}
    data_bytes = json.dumps(data, ensure_ascii=False).encode() if data else b""
    if data_bytes:
        header["data_length"] = len(data_bytes)
    if payload:
        header["payload_length"] = len(payload)
    return json.dumps(header, ensure_ascii=False).encode() + b"\n" + data_bytes + payload


def _length(header: dict, key: str) -> int:
    """A declared section length, validated. 0 for absent/unusable/oversized.

    The value is attacker-controlled and fed straight to readexactly, so it is
    bounded here — an unbounded claim would grow the reader's buffer for as
    long as a slow client keeps dribbling bytes.
    """
    raw = header.get(key)
    if raw is None or isinstance(raw, bool) or not isinstance(raw, int):
        return 0
    return raw if 0 < raw <= MAX_SECTION_BYTES else 0


async def _read_event(reader: asyncio.StreamReader):
    """Read one event; returns (type, data, payload) or None at EOF/garbage.

    Every malformed shape returns None (closing that one connection) rather
    than raising: this is a peer-facing parser on an unauthenticated port, so
    an exception here would surface as an unhandled-task traceback.
    """
    try:
        line = await reader.readline()
    except (ConnectionResetError, asyncio.IncompleteReadError):
        return None
    except ValueError:
        return None  # header line exceeded the reader's limit, no newline in sight
    if not line:
        return None
    try:
        header = json.loads(line)
    except ValueError:
        return None  # not Wyoming framing
    if not isinstance(header, dict):
        return None
    etype = header.get("type")
    if not isinstance(etype, str):
        return None

    inline = header.get("data")
    data = dict(inline) if isinstance(inline, dict) else {}
    try:
        if data_length := _length(header, "data_length"):
            extra = json.loads(await reader.readexactly(data_length))
            if isinstance(extra, dict):
                data.update(extra)
        payload = b""
        if payload_length := _length(header, "payload_length"):
            payload = await reader.readexactly(payload_length)
    except (asyncio.IncompleteReadError, ValueError, TypeError, ConnectionResetError):
        return None
    return etype, data, payload


# ---- info ---------------------------------------------------------------

def _info_data() -> dict:
    from server.app import asr, SERVER_VERSION

    attribution = {"name": "Blurt", "url": "https://blurtvoice.com"}
    # Everything about the model comes off the engine (server/engine.py), so
    # Home Assistant's picker shows what is actually loaded — including the
    # languages, which differ sharply between the two engines.
    return {
        "asr": [{
            "name": "blurt",
            "description": f"Blurt — local dictation server ({asr.engine})",
            "attribution": attribution,
            "installed": True,
            "version": SERVER_VERSION,
            "supports_transcript_streaming": True,
            "models": [{
                "name": asr.model_name,
                "description": asr.description,
                "attribution": asr.attribution,
                "installed": True,
                "version": asr.model_version,
                "languages": list(asr.languages),
            }],
        }],
    }


# ---- connection handler -------------------------------------------------

class _Connection:
    """One Wyoming peer: translates Session events to Wyoming and back."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        from server.app import Session  # late import to avoid a module cycle

        self.reader = reader
        self.writer = writer
        self.session = Session(self._on_session_event)
        self.converter = None          # built from the first audio event's format
        self._sent_committed = ""      # committed text already streamed as chunks
        self._results_open = False     # transcript-start sent, -stop pending

    async def _write(self, etype: str, data: dict | None = None, payload: bytes = b""):
        try:
            self.writer.write(_encode(etype, data, payload))
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def _open_results(self):
        if not self._results_open:
            self._results_open = True
            await self._write("transcript-start")

    async def _close_results(self, text: str = ""):
        """Terminate an open result stream. Every transcript-start gets exactly
        one matching transcript-stop, with the `transcript` Wyoming consumers
        treat as authoritative in between."""
        if self._results_open:
            await self._write("transcript", {"text": text})
            await self._write("transcript-stop")
            self._results_open = False

    async def _on_session_event(self, event: dict):
        """Session -> Wyoming translation (the Session's `emit` callback)."""
        etype = event.get("type")
        if etype == "vad":
            await self._write("voice-started" if event.get("speech") else "voice-stopped")
        elif etype == "partial":
            # Stream newly committed text as append-only chunks. The live
            # (still-revisable) segment has no Wyoming representation — chunk
            # semantics are append-only — so it is intentionally not sent.
            committed = event.get("committed", "")
            if committed and committed != self._sent_committed:
                delta = (committed[len(self._sent_committed):]
                         if committed.startswith(self._sent_committed) else committed)
                self._sent_committed = committed
                await self._open_results()
                await self._write("transcript-chunk", {"text": delta})
        elif etype == "final":
            await self._open_results()
            await self._close_results(event.get("text", ""))
        elif etype == "status" and event.get("state") == "error":
            await self._write("error", {"text": event.get("detail", "transcription failed"),
                                        "code": "stt-error"})
            # A peer waiting on a transcript (Home Assistant's STT provider
            # loops until one arrives) would hang on the error alone, so close
            # the stream out with an empty result.
            await self._close_results("")

    def _converter_for(self, data: dict):
        """(Re)build the PCM converter for a declared format.

        Only fields actually present are honored. Wyoming peers may send the
        format on `audio-start` and omit it on the chunks that follow; treating
        those omissions as a declaration of 16 kHz would silently swap in a
        passthrough converter mid-stream and garble the audio.
        """
        from server.pcm import PcmConverter

        base = self.converter
        rate = int(data["rate"]) if "rate" in data else (base.rate if base else 16000)
        width = int(data["width"]) if "width" in data else (base.width if base else 2)
        channels = int(data["channels"]) if "channels" in data else (base.channels if base else 1)
        if base is None or not base.matches(rate, width, channels):
            self.converter = PcmConverter(rate=rate, width=width, channels=channels)
        return self.converter

    async def run(self):
        from server.pcm import UnsupportedFormat

        try:
            while True:
                try:
                    event = await asyncio.wait_for(_read_event(self.reader),
                                                   timeout=IDLE_TIMEOUT_S)
                except asyncio.TimeoutError:
                    break          # idle peer — reclaim the session
                if event is None:
                    break
                etype, data, payload = event

                if etype == "describe":
                    await self._write("info", _info_data())
                elif etype == "transcribe":
                    pass  # language hint ignored — the model auto-detects
                elif etype == "audio-start":
                    try:
                        converter = self._converter_for(data)
                    except (UnsupportedFormat, ValueError, TypeError) as e:
                        await self._write("error", {"text": str(e), "code": "audio-format"})
                        continue
                    # A peer restarting the stream abandons whatever was in
                    # flight. Close out its result stream first so a second
                    # transcript-start never nests inside the first.
                    await self._close_results(self._sent_committed)
                    await self.session.start(converter=converter)
                    self._sent_committed = ""
                elif etype == "audio-chunk":
                    try:
                        converter = self._converter_for(data)
                    except (UnsupportedFormat, ValueError, TypeError) as e:
                        await self._write("error", {"text": str(e), "code": "audio-format"})
                        continue
                    if not self.session.running:
                        # Peers may skip audio-start and just send chunks.
                        await self.session.start(converter=converter)
                        self._sent_committed = ""
                        self._results_open = False
                    else:
                        self.session.converter = converter
                    self.session.add_audio(payload)
                elif etype == "audio-stop":
                    await self.session.stop()   # -> transcript events via _on_session_event
                elif etype == "ping":
                    await self._write("pong", {"text": data.get("text")} if data.get("text") else None)
                # anything else (tts, wake, intent, ...): ignored by convention
        except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            # One misbehaving peer must not surface as an unhandled task
            # traceback, and must not take anything else down with it.
            print(f"[blurtd] wyoming: connection dropped ({type(e).__name__}: {e})",
                  flush=True)
        finally:
            self.session.abort()
            try:
                self.writer.close()
                await self.writer.wait_closed()
            except Exception:
                pass


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    # asyncio does not install an exception handler for connection callbacks,
    # so anything escaping here becomes an unhandled-task traceback.
    try:
        await _Connection(reader, writer).run()
    except Exception as e:
        print(f"[blurtd] wyoming: handler error ({type(e).__name__}: {e})", flush=True)


async def start_wyoming(host: str, port: int) -> asyncio.base_events.Server:
    """Start the Wyoming TCP listener; returns the asyncio server handle."""
    return await asyncio.start_server(_handle, host, port)
