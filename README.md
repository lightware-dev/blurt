<img src="clients/mac/AppIcon.svg" width="28" align="top" alt=""> Blurt
======

**[blurtvoice.com](https://blurtvoice.com)**

**Talk faster than you type.** A high-performance, fully-local speech-to-text
dictation system — hold a hotkey, say your piece, and the transcript lands in
whatever field you're already in, fast enough to finish before you do. Your
voice never leaves your LAN.

Three parts:

- **`server/`** — a lean NVIDIA **Parakeet** streaming ASR server (Python + NeMo)
  that runs on any modern NVIDIA GPU (developed on an RTX 5090). Modest VRAM
  (~2.3 GB, bf16), low WER, near-realtime live partials over a WebSocket.
- **`clients/mac/`** — a native **macOS menu-bar app** (Swift, universal
  arm64 + x86_64). A global hotkey toggles dictation; live text shows in a HUD;
  the final transcript is typed into whatever field has focus.
- **`clients/windows/`** — a native **Windows tray app** (.NET 8 / WPF). The
  twin of the Mac client: same hotkey-to-dictate flow, same server protocol, a
  live HUD with audio waveform, and text injected into the focused field.

```
 mic ─▶ AVAudioEngine (16 kHz PCM16) ─▶ WebSocket ─▶ Parakeet server (5090)
                                                        │  Silero VAD → segment
 double-tap ⌥ toggles ◀── HUD partials / final text ◀──┘  re-decode every ~350ms
        │
        └▶ inject final text into the focused field (paste, or type)
```

## Quick start

Blurt runs across **two machines on the same LAN**: the **server** on your
NVIDIA GPU box, and a **client** on the Mac or Windows machine you dictate from.
(They can be the same machine if it has the GPU — the client just points at
`localhost`.)

**1 — Start the server** on the GPU box (Linux + NVIDIA GPU). With Docker
(TLS and model download are automatic — see [**Docker**](#docker)):

```bash
docker build -t blurtd .
docker run --gpus all -p 25878:25878 -v blurt-cache:/root/.cache blurtd
```

or from source:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt           # torch must match your CUDA (5090 → cu130)
./scripts/gen_certs.sh                     # self-signed TLS so browsers/clients get wss://
./blurtd                                   # serves wss://<this-box-ip>:25878/ws
```

The first start downloads the model (~0.6 B params) and caches it; later starts
are fast. Verify it's up by opening `https://<gpu-box-ip>:25878/` in a browser —
you'll get a mic test page. Details, config, and GPU support: [**Server**](#server--blurtd-linux--gpu).

**2 — Install a client** on your everyday machine and point it at the server:

- **macOS** — download `Blurt-macOS.zip`, set the server URL to
  `wss://<gpu-box-ip>:25878/ws`, grant Microphone + Accessibility. See
  [**Mac client**](#mac-client).
- **Windows** — download `Blurt-Windows.zip`, set the same URL. See
  [**Windows client**](#windows-client).

**3 — Dictate.** Press the hotkey (**double-tap ⌥** on Mac, **double-tap Ctrl**
on Windows), speak, release — the transcript types into whatever field has focus.

No GPU box yet? You can still exercise the server end-to-end from any machine
with [`scripts/ws_client_test.py`](#validate-without-a-client).

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
start it downloads a **pre-built bf16 checkpoint** from HuggingFace
(`lightware-dev/parakeet-tdt-0.6b-v3-bf16`, override with `PARAKEET_BF16_REPO`) into
`~/.cache/blurt/`; every start after loads that bf16 file straight onto the GPU — no
fp32 copy is ever fetched or materialised. If no cache and no download are available,
it fails fast rather than falling back to the fp32 checkpoint. Pre-build the cache
offline with `python scripts/build_bf16_ckpt.py`.

Serves `wss://<ip>:25878/ws` when `certs/cert.pem` + `certs/key.pem` exist,
otherwise plain `ws://`. The `certs/` dir is git-ignored and generated per
machine — run `scripts/gen_certs.sh` to mint a self-signed pair for your LAN
(browsers require TLS for mic access; the Mac and Windows clients trust it after
a one-time prompt). Open `https://<ip>:25878/` for a **browser mic test page**.
The default port **`25878`** is a mnemonic — `2-5-8-7-8` spells **BLURT** on a phone
keypad (B→2, L→5, U→8, R→7, T→8). Override it with `--port` or `PORT`.

Config (env or `.env`, all optional): `PARAKEET_BF16_CKPT`, `PARAKEET_BF16_REPO`,
`HOST`, `PORT`, `AUTH_TOKEN`, `SILENCE_MS`, `PARTIAL_INTERVAL_MS`, `MAX_SEGMENT_S`,
`VAD_THRESHOLD`, `VAD_PREROLL_MS`, `VAD_HANGOVER_MS`, `LOG_STATS`. See `.env.example`.
`LOG_STATS` (default on) logs per-dictation metadata — packet count, bytes,
audio duration, segments — never transcript text; set `LOG_STATS=0` to silence it.

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

Client → server: `{"type":"start","id":…,"audio":{…}}`, then binary PCM frames
(16 kHz mono PCM16 by default — other rates/stereo are declared and converted),
then `{"type":"stop","id":…}`. Server → client: `{"type":"info",…}` on connect,
`{"type":"vad","speech":…}` (server-side voice activity),
`{"type":"partial","committed":…,"live":…,"text":…}` (live transcript —
committed segments are stable, the live tail may still be revised),
`{"type":"final","text":…}` (on stop), `{"type":"status",…}`. Full reference,
message-by-message: [**docs/protocol.md**](docs/protocol.md).

### OpenAI-compatible API

The same port also serves `POST /v1/audio/transcriptions` (and `GET
/v1/models`), so OpenAI SDKs and tools can transcribe files against Blurt by
pointing `base_url` at `https://<gpu-box-ip>:25878/v1` — `json`, `text`, `srt`,
`vtt`, `verbose_json`, and SSE streaming are all supported; compressed formats
(mp3/m4a/webm/…) decode via ffmpeg. With `AUTH_TOKEN` set, pass it as the API
key. Details in [docs/protocol.md](docs/protocol.md#openai-compatible-api).

### Home Assistant (Wyoming)

`blurtd` can also speak the [Wyoming protocol](https://github.com/OHF-Voice/wyoming),
so it plugs straight into Home Assistant as a speech-to-text engine. It's **off
by default** — Wyoming has no auth and no TLS, so an open port there would
bypass `AUTH_TOKEN` entirely. Opt in with `WYOMING_PORT=10300` (or
`./blurtd --wyoming-port 10300`), then in Home Assistant: *Settings → Devices &
Services → Add Integration → Wyoming Protocol*, host = your GPU box, port =
`10300`. Streaming transcription is supported. Bind it narrowly with
`WYOMING_HOST` if you don't want it on every interface. Details in
[docs/protocol.md](docs/protocol.md#wyoming-listener).

### Validate without a client

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
prompted, then **double-tap ⌥** to dictate (or pick ⌥Space / a custom chord in
Settings). See `clients/mac/README.md` for details
(including notarized distribution).

## Windows client

A native .NET 8 / WPF tray app — the Windows twin of the Mac client, same server
protocol and behaviour. Grab the latest signed build from the
[**Releases page**](https://github.com/lightware-dev/blurt/releases/latest), or the
stable URL:

```
https://github.com/lightware-dev/blurt/releases/latest/download/Blurt-Windows.zip
```

Build from source (needs the .NET 8 SDK):

```bash
cd clients/windows
dotnet publish -c Release      # → publish/Blurt.exe
```

On first run, point Blurt at your server URL, pick a hotkey (default: double-tap
Ctrl), and dictate. Unlike macOS, Windows needs no Accessibility permission for
text injection. See `clients/windows/README.md` for details.

## Contributing

Issues and pull requests are welcome at
[github.com/lightware-dev/blurt](https://github.com/lightware-dev/blurt) — bug
reports, GPU-compatibility data points, and client polish especially. The three
parts build independently: `server/` (Python), `clients/mac/` (Swift), and
`clients/windows/` (.NET). See `AGENTS.md` for repo conventions.

## Files

```
server/            Parakeet streaming server (asr, vad, app, models, __main__)
clients/mac/       Swift menu-bar app + build-app.sh + notarize.sh
clients/windows/   .NET 8 / WPF tray app + Blurt.csproj
www/               marketing site (Next.js) for blurtvoice.com
static/            browser mic test page (index.html, pcm-worklet.js)
scripts/           verify_asr.py, ws_client_test.py, generate_samples.py, gen_certs.sh
audio/             sample wavs
certs/             self-signed TLS for wss:// (git-ignored; run scripts/gen_certs.sh)
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
