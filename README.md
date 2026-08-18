<img src="clients/mac/AppIcon.svg" width="28" align="top" alt=""> Blurt
======

**[blurtvoice.com](https://blurtvoice.com)**

**Talk faster than you type.** A high-performance, fully-local speech-to-text
dictation system — hold a hotkey, say your piece, and the transcript lands in
whatever field you're already in, fast enough to finish before you do. Your
voice never leaves your LAN.

Three parts:

- **`server/`** — a lean NVIDIA **Parakeet** streaming ASR server (Python + NeMo)
  that runs on any modern NVIDIA GPU (Turing or newer). Modest VRAM
  (~2.3 GB, bf16), low WER, near-realtime live partials over a WebSocket.
  **Whisper** is available as an alternative engine (one runs at a time) when you
  need its ~100 languages — see [**Choosing the engine**](#choosing-the-engine--parakeet-or-whisper).
- **`clients/mac/`** — a native **macOS menu-bar app** (Swift, universal
  arm64 + x86_64). A global hotkey toggles dictation; live text shows in a HUD;
  the final transcript is typed into whatever field has focus.
- **`clients/windows/`** — a native **Windows tray app** (.NET 8 / WPF). The
  twin of the Mac client: same hotkey-to-dictate flow, same server protocol, a
  live HUD with audio waveform, and text injected into the focused field.

```
 mic ─▶ AVAudioEngine (16 kHz PCM16) ─▶ WebSocket ─▶ Parakeet server (NVIDIA GPU)
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
docker run --gpus all -p 25878:25878 -v blurtd-cache:/home/blurt/.cache \
    ghcr.io/lightware-dev/blurt:latest
```

or from source:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt           # torch must match your CUDA (cu130 covers RTX 20–50)
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
  tops the ASR accuracy leaderboard, only ~0.6B params. On an RTX 5090 it decodes at
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
pip install -r requirements.txt          # torch must match your CUDA (cu130 covers RTX 20–50)

./blurtd                                  # serve parakeet-tdt-0.6b-v3 (bf16, GPU)
./blurtd --port 8000                      # on another port
./blurtd --engine whisper                 # serve Whisper instead
```

The default model is **`parakeet-tdt-0.6b-v3`**, run in bf16 on the GPU (fp16 on
pre-Ampere cards — see [below](#pre-ampere-gpus--fp16); 4-bit
[nvfp4](#tight-on-vram--nvfp4) if you are short of VRAM). **Whisper** is the
alternative engine — see [**Choosing the engine**](#choosing-the-engine--parakeet-or-whisper).
On first
start it downloads a **pre-built bf16 checkpoint** from HuggingFace
([`lightware-dev/parakeet-tdt-0.6b-v3`](https://huggingface.co/lightware-dev/parakeet-tdt-0.6b-v3),
override with `PARAKEET_REPO`) into
`~/.cache/blurt/`; every start after loads that bf16 file straight onto the GPU — no
fp32 copy is ever fetched or materialised. If no cache and no download are available,
it fails fast rather than falling back to the fp32 checkpoint. Pre-build the cache
offline with `python scripts/build_bf16_ckpt.py`.

Serves `wss://<ip>:25878/ws` when `certs/cert.pem` + `certs/key.pem` exist
(`BLURT_CERT_DIR` moves that directory — the container uses it to keep the cert
on its cache volume), otherwise plain `ws://`. The `certs/` dir is git-ignored and generated per
machine — run `scripts/gen_certs.sh` to mint a self-signed pair for your LAN
(browsers require TLS for mic access; the clients pin it — see
[**Certificate trust**](#certificate-trust)). Open `https://<ip>:25878/` for a
**browser mic test page**.
The default port **`25878`** is a mnemonic — `2-5-8-7-8` spells **BLURT** on a phone
keypad (B→2, L→5, U→8, R→7, T→8). Override it with `--port` or `PORT`.

### Choosing the engine — Parakeet or Whisper

Blurt ships two ASR engines and runs **one per process**. Parakeet is the
default and what the numbers on this page were measured with; `whisper` swaps in
OpenAI's Whisper for the languages Parakeet doesn't cover, or when you want
speech in one language typed out in English.

```bash
./blurtd --engine whisper                 # or BLURT_ASR_ENGINE=whisper ./blurtd
WHISPER_MODEL=openai/whisper-large-v3 WHISPER_LANGUAGE=ja ./blurtd --engine whisper
```

|                | `parakeet` (default) | `whisper` |
| -------------- | -------------------- | --------- |
| Model | `nvidia/parakeet-tdt-0.6b-v3` | `openai/whisper-large-v3-turbo` (any Hub checkpoint via `WHISPER_MODEL`) |
| Params / VRAM | 0.6 B, ~1.4 GB bf16 | 0.8 B, ~1.6 GB bf16 |
| Languages | 25, European | ~100 |
| Precisions | bf16, fp16, [nvfp4](#tight-on-vram--nvfp4) | bf16, fp16 |
| Speed | faster (a TDT decoder emits several frames per step) | slower, still well inside realtime for dictation |
| Extras | — | `WHISPER_TASK=translate` writes English from any input |

Both are driven by the same VAD segmentation, serve the same three protocols,
and are interchangeable to every client — the engine shows up only in the `info`
message's `model` field. Why one at a time: two resident models double the VRAM
this server is built to keep small, and the choice is a property of the box, not
of the request.

Whisper runs through 🤗 transformers on the torch build already in the image, so
there is nothing extra to install; its weights come from the HuggingFace cache
(`~/.cache/huggingface`) on first use, not from the pre-built `.nemo` files
Parakeet loads. Whisper's encoder sees a fixed 30 s window, so a dictation
longer than that is decoded sequentially and stitched — automatic, and slower
per second of audio than a short one.

Whisper pads short audio out to its 30 s window and, on a near-silent fragment,
sometimes fills the rest with a stock phrase from its training data rather than
returning nothing. The VAD in front of it is what prevents that — if filler text
appears in a quiet room, raise `VAD_THRESHOLD` (0.6–0.7) and `MIN_SEGMENT_S`.
Parakeet is much less prone to it, and the defaults were tuned on Parakeet.

Language detection is per dictation and automatic on both engines. On Whisper it
runs off the first window, which is where it goes wrong on a short, noisy start —
pin `WHISPER_LANGUAGE=en` if you always dictate in one language. `WHISPER_DTYPE`
is the fp16 escape hatch for pre-Ampere cards, exactly like `PARAKEET_DTYPE`
below; there is no 4-bit option for Whisper.

#### Pre-Ampere GPUs — fp16

bf16 needs Ampere (sm_80) or newer. A GTX 16xx / RTX 20xx (sm_75) has no bf16 at
all, so `blurtd` refuses to start on one. Those cards run the same model in fp16:

```bash
PARAKEET_DTYPE=fp16 ./blurtd
```

The fp16 checkpoint ships in the same HuggingFace repo as the bf16 one and is
downloaded on first start, exactly like the default path. To build it locally
instead, `python scripts/build_bf16_ckpt.py --dtype fp16` (~20 s, casts from the
fp32 weights). bf16 remains the default and is untouched by this — the two
checkpoints live side by side in `~/.cache/blurt/`.

**fp16 costs nothing here.** Over 208 clips / 25.8 min (100 LibriSpeech test-clean
utterances, the same utterances degraded with white noise at 10 and 5 dB SNR,
babble at 5 dB and near-clipping gain, plus 8 synthetic clips), against an fp32
reference of the same weights:

| | WER, all 208 | WER, real speech | VRAM (weights / peak) | RTF, RTX 5090 |
|---|---|---|---|---|
| fp32 | 2.96% | 2.40% | 2.55 / 2.82 GB | 0.0114 |
| bf16 | 2.99% | 2.40% | 1.31 / 1.43 GB | 0.0133 |
| fp16 | 2.96% | 2.40% | 1.31 / 1.43 GB | 0.0118 |

bf16 and fp16 return byte-identical transcripts on 207 of 208 clips; the WER
difference is +0.03 pp overall (95% CI [0.00, +0.08], p=0.74) and exactly zero on
real speech. Numerically fp16 is the *closer* of the two to fp32 — mean relative
L2 error of the encoder output is 0.005 (cosine ≥ 0.995) against bf16's 0.044
(cosine ≥ 0.835), which is what fp16's 3 extra mantissa bits buy. Nothing overflows
fp16's narrower range: no non-finite activation appeared anywhere, including on the
deliberately near-clipping clips. Reproduce with `scripts/make_eval_corpus.py` and
`scripts/compare_precision.py`.

Measured on an RTX 5090 (sm_120), which supports both formats — the quality
figures carry to sm_75 (IEEE fp16, fp32 accumulation in cuBLAS), the speed figures
do not. A 6 GB GTX 1660 has room for the 1.43 GB peak several times over.

#### Tight on VRAM — nvfp4

`PARAKEET_DTYPE=nvfp4` runs the encoder's weights in **NVFP4**, NVIDIA's 4-bit
float: E2M1 values in blocks of 16, each block carrying its own fp8 scale.
Activations stay bf16, so this needs an Ampere-or-newer card like the default.

```bash
PARAKEET_DTYPE=nvfp4 ./blurtd
```

**It halves memory and costs latency, not accuracy.** Same corpus and method as
the fp16 table above:

| | WER, all 208 | vs bf16 | VRAM (weights / peak) | RTF, RTX 5090 |
|---|---|---|---|---|
| bf16 | 2.99% | — | 1.31 / 1.43 GB | 0.0134 |
| nvfp4 | 2.86% | −0.13 pp, 95% CI [−0.69, +0.30] | 0.51 / 0.78 GB | 0.0316 |

The WER difference is not significant (p=0.69) — nvfp4 is *not* better than bf16,
the corpus simply cannot tell them apart, and at 208 clips it could not resolve a
difference below about 0.3 pp anyway. What it can see is that four bits changes
the exact text of 14% of clips while leaving the aggregate untouched, and that the
encoder output drifts 5x further from fp32 than bf16 does (relative L2 0.226
against 0.044) without a single non-finite activation.

The cost is speed: **2.4x the decode latency**. Parakeet's encoder is bound by
kernel launches rather than arithmetic — a 2.2 s clip and a 35.6 s clip both take
about 27 ms — so cheaper multiplies buy nothing and the extra unpack per layer is
pure overhead. At RTF 0.032 that is still ~30x faster than real time, which is
imperceptible for dictation, but do not reach for nvfp4 expecting throughput.

Unlike bf16 and fp16, this checkpoint is **not a cast** — 4-bit scales are chosen
by calibrating on real audio, so it ships pre-quantized as a snapshot directory
(packed weights in safetensors, recipe in JSON, no pickles anywhere). It downloads
on first start like the others. The GPU never holds a bf16 copy: packed tensors go
straight onto the device, peaking at 0.78 GB against the 2.53 GB a load-time
quantization would need — which is the whole point, since a card that small could
not have quantized the model itself. Rebuild it with
`python scripts/build_nvfp4_snapshot.py --calib <corpus> --verify` (~35 s, needs a
GPU and a calibration corpus from `scripts/make_eval_corpus.py`); `--verify`
reloads the result and requires it to reproduce the transcripts it was built with,
exactly.

Config (env or `.env`, all optional): `BLURT_ASR_ENGINE`, `PARAKEET_DTYPE`, `PARAKEET_BF16_CKPT`,
`PARAKEET_FP16_CKPT`, `PARAKEET_NVFP4_SNAPSHOT`, `PARAKEET_REPO`,
`WHISPER_MODEL`, `WHISPER_DTYPE`, `WHISPER_LANGUAGE`, `WHISPER_TASK`,
`HOST`, `PORT`, `AUTH_TOKEN`, `BLURT_CERT_DIR`, `SILENCE_MS`, `PARTIAL_INTERVAL_MS`, `MAX_SEGMENT_S`,
`VAD_THRESHOLD`, `VAD_PREROLL_MS`, `VAD_HANGOVER_MS`, `LOG_STATS`. See `.env.example`.
`LOG_STATS` (default on) logs per-dictation metadata — packet count, bytes,
audio duration, segments — never transcript text; set `LOG_STATS=0` to silence it.

### Docker

Every release publishes a prebuilt GPU image to the GitHub Container Registry,
so running the server needs neither a checkout nor a local build of the CUDA +
NeMo dependency tree (several GB of wheels):

```bash
docker run --gpus all -p 25878:25878 -v blurtd-cache:/home/blurt/.cache \
    ghcr.io/lightware-dev/blurt:latest
#   append daemon flags after the image:  docker run … ghcr.io/…/blurt:latest --port 8000
```

| Tag | What it is |
| --- | --- |
| `:0.3` | a specific release — pin this if you want upgrades to be a decision |
| `:latest` | the newest release |
| `:edge` | whatever `main` builds right now; no promises |

The image is `linux/amd64` only, on purpose: it exists to run CUDA on an NVIDIA
box, and the `torch` cu130 wheels it installs are x86_64 Linux.

Each published image carries a signed provenance attestation binding it to the
commit and workflow run that built it — the same guarantee the macOS and Windows
downloads get. Verify one straight from the registry:

```bash
gh attestation verify oci://ghcr.io/lightware-dev/blurt:latest --repo lightware-dev/blurt
```

Or build it yourself from a checkout — a `Dockerfile` and `docker-compose.yml`
ship in the repo:

```bash
docker build -t blurtd .
docker run --gpus all -p 25878:25878 -v blurtd-cache:/home/blurt/.cache blurtd
# or:
docker compose up --build
```

Either way the image installs `torch==2.12.1+cu130` from the PyTorch index — the
cu130 wheels bundle the CUDA + cuDNN runtime, so there's no CUDA base image; the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
injects your host driver at run time (`--gpus all`).

Models are pulled from HuggingFace on first run into `/home/blurt/.cache` — the
`blurtd-cache` volume above persists them so you don't re-download on restart.

The daemon runs as the unprivileged `blurt` user (uid 10001) rather than root.
The Compose volume carries an explicit `name:`, so it is `blurtd-cache` under
both `docker compose` and `docker run` — Compose would otherwise prefix it with
the checkout directory's name and the two would disagree.

### Upgrading from an image older than the non-root change

Two things moved at once: the mount point (`/root/.cache` → `/home/blurt/.cache`)
and the volume name (`blurt-cache`, or `<dir>_blurt-cache` under Compose →
`blurtd-cache`). The old volume's contents are also root-owned, so the container
stops on start with:

```
mkdir: cannot create directory '/home/blurt/.cache/blurt-certs': Permission denied
```

Start fresh and re-download the model, or carry the old cache across in one
step. Find the old volume first — `docker run -v` against a name that doesn't
exist *silently creates an empty volume* instead of failing, so copying from the
wrong name looks like it worked and gets you nothing:

```bash
docker volume ls | grep cache
```

Then copy it into the new one, taking ownership as you go:

```bash
docker run --rm -v <old-volume>:/from:ro -v blurtd-cache:/to alpine \
    sh -c 'cp -a /from/. /to/ && chown 10001:10001 /to'
```

Bring it up and confirm — the daemon should be uid 10001, and the model should
load from the cache rather than downloading again:

```bash
docker compose up --build -d
docker exec blurtd id     # uid=10001(blurt) gid=10001(blurt)
```

Keeping the old cache also preserves the auto-generated TLS cert, so browsers
and clients won't re-prompt. Once you're happy, `docker volume rm <old-volume>`.

**TLS is automatic.** Browsers block LAN mic access over plain `ws://`, so the
entrypoint auto-generates a self-signed cert on first start and serves `wss://`
out of the box (the Mac client trusts it; a browser prompts once). The cert also
lives in `blurtd-cache`, so its fingerprint is stable across restarts. Bring your
own by mounting `-v ./certs:/app/certs:ro`, or set `BLURT_AUTOCERT=0` to fall
back to `ws://`. Full smoke test once it's up: `python scripts/ws_client_test.py
audio/clean.wav`.

**Supported GPUs.** The server — image or source install — runs on any NVIDIA consumer card from the
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

Releases carry a signed [build provenance
attestation](https://docs.github.com/actions/security-guides/using-artifact-attestations)
binding the download to the workflow run and commit that built it — notarization
proves Apple saw the binary, not which source tree it came from. To check:

```bash
gh attestation verify Blurt-macOS.zip --repo lightware-dev/blurt
```

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
protocol and behaviour. Grab the latest build from the
[**Releases page**](https://github.com/lightware-dev/blurt/releases/latest), or the
stable URL:

```
https://github.com/lightware-dev/blurt/releases/latest/download/Blurt-Windows.zip
```

Windows builds are **not** Authenticode-signed, so SmartScreen warns on first
launch — choose *More info* → *Run anyway*. They do carry a signed [build
provenance attestation](https://docs.github.com/actions/security-guides/using-artifact-attestations),
which is currently the only cryptographic link between the zip and this
repository:

```bash
gh attestation verify Blurt-Windows.zip --repo lightware-dev/blurt
```

Build from source (needs the .NET 8 SDK):

```bash
cd clients/windows
dotnet publish -c Release      # → publish/Blurt.exe
```

On first run, point Blurt at your server URL, pick a hotkey (default: double-tap
Ctrl), and dictate. Unlike macOS, Windows needs no Accessibility permission for
text injection. See `clients/windows/README.md` for details.

## Certificate trust

Both clients authenticate the server's TLS certificate, and neither will talk to
a `wss://` server it can't vouch for. What happens depends on the certificate:

- **Signed by a real CA** — connects silently, nothing to confirm. Nothing is
  pinned, so ordinary renewals keep working.
- **Self-signed, first time for this server** — Blurt shows the host and the
  certificate's SHA-256 fingerprint and asks you to confirm it once. Say yes and
  it's pinned; from then on that server connects silently.
- **Self-signed, and the fingerprint changed** — a louder warning, defaulting to
  *Cancel*. Confirm only if you re-ran `gen_certs.sh` yourself; otherwise
  something is impersonating your server.

Pins are per `host:port`, so several servers (and `localhost` vs. a LAN address)
are tracked independently — on macOS in `UserDefaults`, on Windows in
`%APPDATA%\Blurt\config.json`. To compare a fingerprint against the server:

```bash
openssl x509 -in certs/cert.pem -noout -fingerprint -sha256
```

The check runs when the app launches and whenever you change the server URL, so
the dialog never lands on top of a live dictation and eats what you were saying.
A plain `ws://` server has no certificate and is unaffected.

## Contributing

Issues and pull requests are welcome at
[github.com/lightware-dev/blurt](https://github.com/lightware-dev/blurt) — bug
reports, GPU-compatibility data points, and client polish especially. The three
parts build independently: `server/` (Python), `clients/mac/` (Swift), and
`clients/windows/` (.NET). See `AGENTS.md` for repo conventions.

## Files

```
server/            streaming server (app, engine, asr/whisper, vad, pcm, openai_api, wyoming)
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
