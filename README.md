# 🦜 local-voice — Parakeet dictation

A high-performance, local speech-to-text dictation system:

- **`server/`** — a lean NVIDIA **Parakeet** streaming ASR server (Python + NeMo)
  that runs on this Linux box's RTX 5090. Minimal VRAM (~1.5 GB, bf16), low WER,
  near-realtime live partials over a WebSocket.
- **`client-mac/`** — a native **macOS menu-bar app** (Swift, universal
  arm64 + x86_64). A global hotkey toggles dictation; live text shows in a HUD;
  the final transcript is typed into whatever field has focus.

```
 mic ─▶ AVAudioEngine (16 kHz PCM16) ─▶ WebSocket ─▶ Parakeet server (5090)
                                                        │  Silero VAD → segment
 ⌥Space toggles ◀── HUD partials / final text ◀────────┘  re-decode every ~350ms
        │
        └▶ inject final text into the focused field (paste, or type)
```

## Why this design

- **Model:** `parakeet-tdt-0.6b-v3` — multilingual (English + Portuguese + 23 more),
  tops the ASR accuracy leaderboard, only ~0.6B params. On the 5090 it decodes at
  **RTF ~0.002–0.01** (a 35 s clip in ~80 ms) using **~1.4 GB VRAM**.
- **VAD-segmented streaming:** audio is split into utterances at silences (Silero
  VAD). Each active segment is re-decoded every ~350 ms for live partials and
  committed on a pause. VRAM is bounded by the *longest single utterance*, not the
  session length, and every decode sees full segment context for low WER.
- **VRAM optimizations:** bf16 by default (fp32 opt-out), `torch.inference_mode`,
  `MAX_SEGMENT_S` caps an unbroken utterance, and CUDA cache is released after long
  segments so peak memory doesn't stick.

## Server (Linux + GPU)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # torch must match your CUDA (5090 → cu130)

python -m server                         # default model (v3, multilingual)
python -m server --list-models           # see all Parakeet variants
python -m server -m v2 --port 8000       # English-only 0.6B on another port
```

Serves `wss://<ip>:7860/ws` when `certs/cert.pem` + `certs/key.pem` exist (they do),
otherwise plain `ws://`. Open `https://<ip>:7860/` for a **browser mic test page**.

Config (env or `.env`, all optional): `PARAKEET_MODEL`, `PARAKEET_FP32`, `HOST`,
`PORT`, `AUTH_TOKEN`, `SILENCE_MS`, `PARTIAL_INTERVAL_MS`, `MAX_SEGMENT_S`. See
`.env.example`.

### WebSocket protocol

Client → server: `{"type":"start"}`, then binary 16 kHz mono PCM16 frames, then
`{"type":"stop"}`. Server → client: `{"type":"partial","text":…}` (live),
`{"type":"final","text":…}` (on stop), `{"type":"status",…}`.

### Validate without the Mac

```bash
python scripts/ws_client_test.py audio/clean.wav    # streams a wav, prints partials + final
python scripts/verify_asr.py                         # offline decode + VRAM/RTF report
```

## Mac client

Built on the Mac (needs Xcode command-line tools):

```bash
cd client-mac
./build-app.sh        # universal arm64 + x86_64 → VoiceDictate.app (ad-hoc signed)
open VoiceDictate.app
```

Set the server URL from the menu-bar icon (▸ *Set Server URL…* →
`wss://<linux-ip>:7860/ws`), grant **Microphone** and **Accessibility** when
prompted, then press **⌥Space** to dictate. See `client-mac/README.md` for details.

## Files

```
server/            Parakeet streaming server (asr, vad, app, models, __main__)
client-mac/        Swift menu-bar app + build-app.sh
static/            browser mic test page (index.html, pcm-worklet.js)
scripts/           verify_asr.py, ws_client_test.py, generate_samples.py
certs/             self-signed TLS for wss:// on the LAN
audio/             sample wavs
```
