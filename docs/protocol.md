# The Blurt protocol

How clients talk to `blurtd`. The server speaks three protocols:

- **The native Blurt protocol** — JSON + binary frames over a single
  WebSocket. Used by the Mac client, the Windows client, and the `/` mic-test
  page. This is the primary protocol and the one with auth + TLS.
- **The [Wyoming protocol](https://github.com/OHF-Voice/wyoming)** — the
  peer-to-peer voice protocol used by Home Assistant and Rhasspy, served on a
  second listener so Blurt can act as a Home Assistant STT backend.
  See [Wyoming listener](#wyoming-listener) below.
- **An OpenAI-compatible transcription API** — `POST /v1/audio/transcriptions`
  on the native port, for whole-file transcription from OpenAI SDKs and tools.
  See [OpenAI-compatible API](#openai-compatible-api) below.

The native protocol's implementation lives in `server/app.py`; the Wyoming
listener in `server/wyoming.py`; the OpenAI-compatible API in
`server/openai_api.py`; audio format conversion in `server/pcm.py`.

---

## Native protocol

### Transport

One WebSocket connection per dictation session:

```
wss://<server>:25878/ws            (wss:// when certs are present, else ws://)
wss://<server>:25878/ws?token=<t>  (when the server sets AUTH_TOKEN)
```

- **Text frames** carry JSON messages, one message per frame.
- **Binary frames** carry raw PCM audio, in the format declared by `start`.
- A wrong/missing token closes the socket with WebSocket code **1008**.

The clients open the connection when a dictation starts and close it after the
final text arrives; the protocol also supports several dictations over one
connection (`start` … `stop`, then `start` again).

### Message summary

| Direction | `type` | Purpose |
|---|---|---|
| client → server | `start` | begin a dictation (id + audio format) |
| client → server | *(binary)* | PCM audio frames |
| client → server | `stop` | end the dictation; asks for the final text |
| client → server | `describe` | request a fresh `info` message |
| server → client | `info` | server identity/capabilities (on connect + on `describe`) |
| server → client | `status` | `ready` ack, `loading`, or `error` |
| server → client | `vad` | server-side voice activity on/off |
| server → client | `partial` | live transcript: committed + live segment |
| server → client | `final` | the authoritative full transcript |

Unknown message types and unknown fields must be **ignored by both sides** —
that is what lets client and server be upgraded independently. The `protocol`
number in `info` only bumps on breaking changes.

### Dictation lifecycle

```
client                                server
  │ ──── WebSocket connect ────────────▶ │
  │ ◀──────────────────────── {info} ─── │   who am I talking to?
  │ ─── {start, id, audio} ────────────▶ │
  │ ◀───────────── {status: ready, id} ─ │   ack; audio may flow
  │ ─── PCM ─── PCM ─── PCM ── … ──────▶ │
  │ ◀──────────── {vad: speech on, id} ─ │   server hears speech
  │ ◀──────────────────── {partial, id} ─ │   every ~350 ms while speaking
  │ ◀─────────── {vad: speech off, id} ─ │   pause detected
  │ ◀──────────────────── {partial, id} ─ │   segment committed
  │ ─── {stop, id} ────────────────────▶ │
  │ ◀────────────────────── {final, id} ─ │   full-context re-decode
  │ ──── close ────────────────────────▶ │
```

### The dictation id

`start` carries a client-chosen opaque string `id` (the clients use a UUID).
Every dictation-scoped server message (`status`, `vad`, `partial`, `final`)
echoes it back; `stop` must carry it too.

Why it exists: finalization is asynchronous (the final decode can take a couple
of seconds), so a client that stops one dictation and quickly starts another
could otherwise receive the *previous* dictation's `final` and type it into the
new context. The rules:

- **Clients** drop any message whose `id` is present, non-empty, and not the
  current dictation's id.
- **The server** ignores a `stop` whose `id` doesn't match the active
  dictation. A `stop` with no `id` at all matches whatever is running.
- If `start` has no `id`, the server generates one, so its messages are always
  tagged.
- Messages scoped to the connection, not a dictation (`info`), carry no `id`.

### Client → server messages

#### `start`

```json
{"type": "start", "id": "6ff3…", "audio": {"rate": 16000, "width": 2, "channels": 1}}
```

Begins a dictation. If one is already running on this connection it is
**superseded**: the old dictation is abandoned without a `final`, no further
events are emitted under its id, and the new one starts clean.

`audio` declares the format of the binary frames that will follow:

| field | meaning | accepted values | default |
|---|---|---|---|
| `rate` | sample rate in Hz | 8000 – 192000 | 16000 |
| `width` | bytes per sample | 2 (PCM16 little-endian) | 2 |
| `channels` | channel count | 1, or 2 (downmixed to mono) | 1 |

The server converts whatever is declared to its canonical 16 kHz mono PCM16
(linear resampling with cross-frame continuity — fine for speech; capture at
16 kHz natively when you can). An unusable declaration is answered with
`{"type": "status", "state": "error", "detail": …, "id": …}` and the dictation
does not start. Both `id` and `audio` are optional: a bare
`{"type": "start"}` gets a server-generated id and the 16 kHz default.

#### Binary audio frames

Raw PCM in the declared format, no header, any frame size (the clients send
~50–100 ms per frame). Frames sent while no dictation is running are discarded.

#### `stop`

```json
{"type": "stop", "id": "6ff3…"}
```

Ends the dictation. The server flushes, produces the best-possible final text,
and replies with `final`. Finals for dictations up to `FINAL_MAX_S` (default
120 s) come from a single full-context re-decode of the entire dictation;
beyond that, the committed segments are stitched instead.

`stop` always produces exactly one `final`. If the final decode exceeds
`STOP_TIMEOUT_S` (default 60 s), the server returns the text already committed
rather than leaving the client waiting — a slow decode degrades the transcript,
it never silently discards it. A `stop` whose `id` doesn't match the running
dictation is ignored; a `stop` with no `id` matches whatever is running.

#### `describe`

```json
{"type": "describe"}
```

Asks for a fresh `info` (e.g. to poll whether the model finished loading).

### Server → client messages

#### `info` — sent on connect and on `describe`

```json
{
  "type": "info",
  "protocol": 1,
  "server": "blurtd",
  "version": "0.2",
  "model": "nvidia/parakeet-tdt-0.6b-v3",
  "state": "ready",
  "audio": {"rate": 16000, "width": 2, "channels": 1}
}
```

- `protocol` — breaking-change counter. A client built for 2 works with any
  server reporting 2, whatever else got added since.
- `state` — `ready` when the model is loaded, `loading` before that (first
  dictation will stall until loading finishes; the clients show "Loading
  model…").
- `audio` — the server's canonical format (what you get if you declare
  nothing).

#### `status`

```json
{"type": "status", "state": "ready",  "id": "6ff3…"}
{"type": "status", "state": "error", "detail": "…", "id": "6ff3…"}
```

`ready` acknowledges `start`. `error` reports a fatal problem with the current
dictation (bad audio declaration, a failed decode). **`error` is terminal for
that dictation and no `final` follows it** — clients must treat it as the end
of the dictation, not keep waiting. The connection stays usable: a fresh
`start` after an error begins a new dictation normally.

#### `vad`

```json
{"type": "vad", "speech": true,  "id": "6ff3…"}
{"type": "vad", "speech": false, "id": "6ff3…"}
```

Emitted on transitions of the *server-side* Silero VAD. `speech: true` fires as
soon as speech is detected; `speech: false` only after ~`VAD_OFF_MS` (default
300 ms) of silence, so inter-word gaps don't flap. Clients use it as end-to-end
confirmation that audio is arriving and being heard ("Hearing you…"), which the
local waveform can't prove.

#### `partial` — the live transcript

```json
{
  "type": "partial",
  "committed": "the first two segments already decoded",
  "live": "and the one still being spo",
  "text": "the first two segments already decoded and the one still being spo",
  "id": "6ff3…"
}
```

Sent every ~`PARTIAL_INTERVAL_MS` (default 350 ms) while speech is active, and
once whenever a segment commits.

- `committed` — segments finalized by a pause; **append-only**, never revised.
- `live` — the active segment's current decode; **revised on every partial**
  (each re-decode sees the whole segment, so earlier words can change).
- `text` — always `committed + " " + live`; the full running transcript, for
  clients that just want the whole string. Replace-the-whole-display semantics.

A note on rendering, learned the hard way: text only becomes `committed` after
a ~600 ms pause, so a single uninterrupted utterance — the common dictation —
has an **empty `committed` for its entire duration**. Any styling that
de-emphasises `live` therefore de-emphasises the whole transcript most of the
time. Blurt's clients do the opposite: `live` keeps the full-strength
treatment, and `committed` recedes behind it.

#### `final`

```json
{"type": "final", "text": "the whole dictation, decoded in one pass", "id": "6ff3…"}
```

The authoritative transcript. It supersedes everything streamed in partials —
the full-context re-decode routinely fixes segment-boundary artifacts, so
clients must replace, not append.

### Evolution rules

These exist so client and server can be upgraded independently rather than in
lockstep:

1. Ignore unknown message types; ignore unknown fields in known types.
2. Never remove or re-type an existing field without bumping `protocol`.
3. `id` and `audio` in `start` stay optional; `text` in `partial` stays the
   full running transcript.
4. New capabilities are announced in `info`, requested with new message types.

Following those, a purely additive change needs no version bump — a client
built against `protocol: 1` keeps working against any server that still
reports 1, whatever has been added since.

### Server configuration (env vars)

| var | default | meaning |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `25878` | native listener bind |
| `AUTH_TOKEN` | *(empty)* | if set, `/ws` and `/v1` require it |
| `WYOMING_PORT` | `0` (off) | Wyoming listener port; `10300` to enable |
| `WYOMING_HOST` | *(= `HOST`)* | narrow the Wyoming bind independently |
| `SILENCE_MS` | `600` | pause that commits a segment |
| `PARTIAL_INTERVAL_MS` | `350` | live partial cadence |
| `MIN_SEGMENT_S` / `MAX_SEGMENT_S` | `0.3` / `20` | segment size bounds |
| `FINAL_MAX_S` | `120` | max dictation length for the one-shot final re-decode |
| `STOP_TIMEOUT_S` | `60` | final-decode budget before `stop` returns committed text |
| `MAX_QUEUED_FRAMES` | `4096` | cap on undecoded audio backlog per dictation |
| `VAD_OFF_MS` | `300` | silence debounce before `vad speech:false` |
| `MAX_UPLOAD_MB` / `MAX_AUDIO_S` | `200` / `7200` | `/v1` upload and duration ceilings |
| `LOG_STATS` | `1` | per-dictation metadata logging (never transcript text) |

---

## Wyoming listener

An optional second listener speaks the
[Wyoming protocol](https://github.com/OHF-Voice/wyoming) (JSONL headers +
binary payloads over plain TCP), so anything that can consume a Wyoming ASR
service — most notably **Home Assistant** — can use Blurt for speech-to-text.

> **Off by default, and deliberately so.** Wyoming has no authentication and no
> TLS — that is the ecosystem's norm — so `AUTH_TOKEN` does *not* apply to this
> port. Enabling it opens an unauthenticated path to the same GPU and model the
> token protects everywhere else. Turn it on with `WYOMING_PORT=10300` (the
> ecosystem's ASR convention) or `./blurtd --wyoming-port 10300`, and consider
> `WYOMING_HOST=127.0.0.1` if only a local Home Assistant needs it.

If the port is already taken — likely, since 10300 is shared with
`wyoming-faster-whisper` and friends — the listener logs a warning and is
skipped. Dictation is never affected by it.

### Home Assistant setup

1. Start blurtd with the listener enabled: `WYOMING_PORT=10300 ./blurtd`
   (or add `WYOMING_PORT: "10300"` and the port mapping in docker-compose.yml).
2. In Home Assistant: **Settings → Devices & Services → Add Integration →
   Wyoming Protocol**. Host: your GPU box's IP. Port: `10300`.
3. Blurt appears as an STT provider ("blurt"); select it in **Settings → Voice
   assistants** as the Speech-to-text engine.

Streaming transcription is advertised (`supports_transcript_streaming`), so a
current Home Assistant starts intent-processing from the streamed segments
before the final decode lands.

### Supported events

| Incoming | Behavior |
|---|---|
| `describe` | replies with `info` (asr program + Parakeet model + 25 languages) |
| `transcribe` | accepted; the language hint is ignored (the model auto-detects) |
| `audio-start` | begins a dictation |
| `audio-chunk` | PCM payload; `rate`/`width`/`channels` honored via the same converter as the native protocol (a chunk before `audio-start` implicitly starts) |
| `audio-stop` | finalizes and emits the transcript events |
| `ping` | replies with `pong` |
| anything else | ignored, per the Wyoming convention |
| malformed framing | that one connection is closed; the listener stays up |

A peer that sends `audio-start` again without an intervening `audio-stop`
abandons the dictation in flight: the open result stream is closed out
(`transcript` + `transcript-stop`) before a new one begins, so
`transcript-start` and `transcript-stop` always pair up.

| Outgoing | When |
|---|---|
| `info` | in reply to `describe` |
| `voice-started` / `voice-stopped` | mapped from the server-side VAD transitions |
| `transcript-start` | before the first result event |
| `transcript-chunk` | each committed segment, as it commits (append-only) |
| `transcript` | the authoritative final text (full-context re-decode) |
| `transcript-stop` | end of the result stream |
| `error` | decode failure or unusable audio format |

### Mapping between the two protocols

| Native | Wyoming |
|---|---|
| `info` | `describe` → `info` |
| `start` + declared `audio` | `audio-start` (+ per-chunk format fields) |
| binary frames | `audio-chunk` payloads |
| `stop` | `audio-stop` |
| `vad speech:true/false` | `voice-started` / `voice-stopped` |
| `partial.committed` growth | `transcript-chunk` (delta) |
| `partial.live` | *(not sent — Wyoming chunks are append-only; the live segment is still revisable)* |
| `final` | `transcript` (wrapped in `transcript-start`/`-stop`) |
| `status error` | `error` |
| dictation `id` | *(none — Wyoming scopes a request to its TCP connection)* |

---

## OpenAI-compatible API

`POST /v1/audio/transcriptions` on the native port (25878) implements the
OpenAI Audio API's transcription surface, so anything built against OpenAI can
point its `base_url` at Blurt for **whole-file** transcription (the live
streaming path stays the native WebSocket protocol):

```bash
curl -sk https://<server>:25878/v1/audio/transcriptions \
  -H "Authorization: Bearer $AUTH_TOKEN" \
  -F file=@recording.mp3 -F model=whisper-1 -F response_format=srt
```

```python
from openai import OpenAI
client = OpenAI(base_url="https://<server>:25878/v1", api_key="<AUTH_TOKEN>")
text = client.audio.transcriptions.create(
    model="whisper-1", file=open("recording.mp3", "rb")).text
```

### Endpoints

- `POST /v1/audio/transcriptions` — multipart form, fields below.
- `GET /v1/models` — lists the real model plus a `whisper-1` alias, so stock
  model pickers (Open WebUI etc.) validate.

### Request fields

| field | behavior |
|---|---|
| `file` | required; wav/flac/ogg decoded natively, everything else (mp3, m4a, webm, …) via ffmpeg |
| `model` | accepted, ignored — there is one model |
| `language`, `prompt`, `temperature`, `timestamp_granularities` | accepted, ignored (the model auto-detects language) |
| `response_format` | `json` (default), `text`, `srt`, `vtt`, `verbose_json` |
| `stream` | `true` → SSE stream (below) |

### Responses

- `json` → `{"text": …}`; `text` → the bare transcript.
- `verbose_json` → `task`/`language`/`duration`/`text` plus `segments[]` with
  `id`, `start`, `end`, `text` (and neutral values for the other Whisper
  fields).
- `srt` / `vtt` → subtitle cues, one per segment.
- `stream=true` → `text/event-stream` of
  `{"type":"transcript.text.delta","delta":…}` events (one per segment, in
  order) closed by `{"type":"transcript.text.done","text":…}` and `data: [DONE]`
  — the shape the OpenAI SDK's streaming transcription client expects.

Segments come from an offline pass of the same Silero VAD used live: cuts land
in real pauses (never mid-word), but each segment spans up to the next cut, so
cue timestamps include the surrounding silence — treat them as approximate.
Short files requested as plain `json`/`text` skip segmentation entirely and get
the best-quality single full-context decode.

### Auth and errors

If `AUTH_TOKEN` is set, requests need `Authorization: Bearer <AUTH_TOKEN>`
(`?token=` also works, matching the WebSocket). Errors use the OpenAI shape —
`{"error": {"message", "type", …}}` — with 401 for a bad key and 400 for
undecodable audio or an unknown `response_format`.

---

## Tests

`scripts/test_protocol.py` exercises everything on this page — all three
surfaces, plus the malformed-input and lifecycle cases — with the model and the
VAD stubbed, so it runs anywhere without a GPU:

```bash
python scripts/test_protocol.py             # all suites
python scripts/test_protocol.py native      # one suite (native|wyoming|openai|pcm)
```
