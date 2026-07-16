# 🗣️ Blurt

**Say it badly, get it typed well.** A high-performance, fully-local speech-to-text
dictation system — you talk faster than you type, so just let it out and Blurt
cleans it up into the field you're already in. Your voice never leaves your LAN.

Two parts:

- **`server/`** — a lean NVIDIA **Parakeet** streaming ASR server (Python + NeMo)
  that runs on this Linux box's RTX 5090. Modest VRAM (~2.3 GB, bf16), low WER,
  near-realtime live partials over a WebSocket.
- **`clients/mac/`** — a native **macOS menu-bar app** (Swift, universal
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
  **RTF ~0.002–0.01** (a 35 s clip in ~70 ms) using **~2.3 GB VRAM** (bf16,
  measured via `nvidia-smi`; ~1.4 GB of that is live tensors, the rest CUDA
  context + reserved pools).
- **VAD-segmented streaming:** audio is split into utterances at silences (Silero
  VAD). Each active segment is re-decoded every ~350 ms for live partials and
  committed on a pause. VRAM is bounded by the *longest single utterance*, not the
  session length, and every decode sees full segment context for low WER.
- **VRAM optimizations:** bf16 weights loaded straight to the GPU, `torch.inference_mode`,
  `MAX_SEGMENT_S` caps an unbroken utterance, and CUDA cache is released after long
  segments so peak memory doesn't stick.

## Server — `blurtd` (Linux + GPU)

The server half is a daemon called **`blurtd`** — which is, yes, the past tense of
what it does. `./blurtd` is a thin wrapper around `python -m server`.

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt          # torch must match your CUDA (5090 → cu130)

./blurtd                                  # serve parakeet-tdt-0.6b-v3 (bf16, GPU)
./blurtd --port 8000                      # on another port
```

The model is fixed at **`parakeet-tdt-0.6b-v3`**, run in bf16 on the GPU. On first
start it converts the published fp32 checkpoint to bf16 and caches it under
`~/.cache/blurt/`; every start after loads that bf16 file straight onto the GPU.
Pre-build the cache without starting the server with `python scripts/build_bf16_ckpt.py`.

Serves `wss://<ip>:25878/ws` when `certs/cert.pem` + `certs/key.pem` exist (they do),
otherwise plain `ws://`. Open `https://<ip>:25878/` for a **browser mic test page**.
The default port **`25878`** is a mnemonic — `2-5-8-7-8` spells **BLURT** on a phone
keypad (B→2, L→5, U→8, R→7, T→8). Override it with `--port` or `PORT`.

Config (env or `.env`, all optional): `PARAKEET_BF16_CKPT`, `HOST`, `PORT`,
`AUTH_TOKEN`, `SILENCE_MS`, `PARTIAL_INTERVAL_MS`, `MAX_SEGMENT_S`,
`LOG_STATS`. See `.env.example`. `LOG_STATS` (default on) logs per-dictation
metadata — packet count, bytes, audio duration, segments — never transcript text;
set `LOG_STATS=0` to silence it.

### Docker

A `Dockerfile` (and `docker-compose.yml`) ship the daemon as a GPU container.
The image installs `torch==2.12.1+cu130` from the PyTorch index — the cu130
wheels bundle the CUDA + cuDNN runtime, so there's no CUDA base image; the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
injects your host driver at run time (`--gpus all`).

```bash
docker build -t blurtd .
docker run --gpus all -p 25878:25878 -v blurt-cache:/root/.cache blurtd
#   append flags like a bare invocation:  docker run … blurtd --port 8000
# or:
docker compose up --build
```

Models are pulled from HuggingFace on first run into `/root/.cache` — the
`blurt-cache` volume above persists them so you don't re-download on restart.

**TLS is automatic.** Browsers block LAN mic access over plain `ws://`, so the
entrypoint auto-generates a self-signed cert on first start and serves `wss://`
out of the box (the Mac client trusts it; a browser prompts once). The cert also
lives in `blurt-cache`, so its fingerprint is stable across restarts. Bring your
own by mounting `-v ./certs:/app/certs:ro`, or set `BLURT_AUTOCERT=0` to fall
back to `ws://`. Full smoke test once it's up: `python scripts/ws_client_test.py
audio/clean.wav`.

**Supported GPUs.** The image runs on any NVIDIA consumer card from the
**RTX 20-series (and GTX 16-series) through the RTX 50-series** — the torch
wheel carries native `sm_75/86/90/100/120` kernels, and CUDA minor-version
compatibility covers Ada (RTX 40-series) via the `sm_86` binaries.

| Architecture | Consumer GPUs                        | Runs via              |
| ------------ | ------------------------------------ | --------------------- |
| Turing       | GTX 1650/1660, RTX 2060–2080 Ti      | native `sm_75`        |
| Ampere       | RTX 3050–3090 Ti                     | native `sm_86`        |
| Ada Lovelace | RTX 4060–4090                        | `sm_86` (minor-compat)|
| Blackwell    | RTX 5060–5090                        | native `sm_120`       |

Two host-side caveats: the bundled CUDA 13.0 runtime needs **driver ≥ 580**
(upgrade even a listed card on an older branch), and **Pascal and older**
(GTX 10-series, Titan V) are unsupported — no matching kernel, so they fail with
a "no kernel image" error. VRAM is a non-issue: the models are ~0.5–2.5 GB.

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

### Download

Grab the latest signed + notarized build from the
[**Releases page**](https://github.com/lightware-dev/blurt/releases/latest), or link
straight to the stable URL:

```
https://github.com/lightware-dev/blurt/releases/latest/download/Blurt-macOS.zip
```

Unzip, drag **Blurt.app** to `/Applications`, and open it — it's a universal
(Apple Silicon + Intel) menu-bar app that launches without Gatekeeper warnings.

### Build from source

Built on the Mac (needs Xcode command-line tools):

```bash
cd clients/mac
./build-app.sh        # universal arm64 + x86_64 → Blurt.app (Developer ID or ad-hoc signed)
open Blurt.app

./notarize.sh         # ship it: build → notarize → staple → dist/Blurt-<version>.zip
```

Set the server URL from the menu-bar icon (▸ *Set Server URL…* →
`wss://<linux-ip>:25878/ws`), grant **Microphone** and **Accessibility** when
prompted, then press **⌥Space** to dictate. See `clients/mac/README.md` for details
(including notarized distribution).

## Files

```
server/            Parakeet streaming server (asr, vad, app, models, __main__)
clients/mac/       Swift menu-bar app + build-app.sh + notarize.sh
static/            browser mic test page (index.html, pcm-worklet.js)
scripts/           verify_asr.py, ws_client_test.py, generate_samples.py
certs/             self-signed TLS for wss:// on the LAN
audio/             sample wavs
Dockerfile         GPU container for blurtd (torch cu130 + NeMo)
docker-compose.yml one-command run with GPU + model-cache volume
```

## License

Licensed under the [Apache License, Version 2.0](LICENSE) — see `LICENSE` for the
full text and `NOTICE` for attribution. © 2026 Lightware Consulting, Lda. Blurt
builds on third-party models and libraries (Parakeet/NeMo, Silero VAD, PyTorch,
FastAPI, Next.js) under their own licenses; see `NOTICE`.

The **Blurt** name, logo, and mascot are trademarks of Lightware Consulting, Lda
and are **not** licensed under Apache 2.0. You may build on and redistribute the
code, but not use the Blurt branding to imply endorsement by or affiliation with
Lightware.
