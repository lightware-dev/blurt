# blurtd — the Blurt (Parakeet) dictation daemon, containerized.
#
# GPU image. The torch cu130 wheels bundle the CUDA + cuDNN runtime, so a plain
# python base is enough — the NVIDIA Container Toolkit injects the host driver at
# run time. Requires `--gpus all` and a driver that supports CUDA 13 (>= 580).
#
#   docker build -t blurtd .
#   docker run --gpus all -p 25878:25878 -v blurtd-cache:/home/blurt/.cache blurtd
#
# Models are pulled from HuggingFace on first run into /home/blurt/.cache —
# mount a volume there (as above) so you don't re-download on every start.
#
# The daemon runs as the unprivileged `blurt` user (uid 10001), not root: it
# binds a high port and needs no capability at run time, and the listeners are
# reachable from the LAN without auth by default, so a bug in the audio decode
# path (ffmpeg, libsndfile, soundfile) should not start out as uid 0.
FROM python:3.12-slim-bookworm

# Runtime libs: ffmpeg + libsndfile for audio I/O (soundfile/librosa),
# git for a couple of pip source deps. build-essential for wheels that compile.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        ffmpeg \
        libsndfile1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Installs torch==2.12.1+cu130 from the PyTorch index first (the cu130 wheels
# bundle the CUDA + cuDNN runtime, so no CUDA base image needed — the NVIDIA
# Container Toolkit injects your host driver at runtime), then the rest of
# requirements.txt (NeMo, fastapi, etc.).
#
# torch is installed first, from the PyTorch index, so NeMo sees it already
# satisfied. The cu130 wheels carry sm_75/86/90/100/120 kernels, covering
# Turing through Blackwell.
RUN pip install --no-cache-dir torch==2.12.1 \
        --index-url https://download.pytorch.org/whl/cu130

# The rest from PyPI (nemo_toolkit[asr], fastapi, uvicorn, silero-vad, …).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# openssl for the entrypoint's self-signed cert. Kept in its own late layer so
# the heavy torch/NeMo layers above stay cached across rebuilds.
RUN apt-get update && apt-get install -y --no-install-recommends openssl \
    && rm -rf /var/lib/apt/lists/*

# App code. certs/ and .env are intentionally not baked in — mount your own
# certs/ to use them, otherwise the entrypoint auto-generates a self-signed cert
# so wss:// works out of the box (set BLURT_AUTOCERT=0 to fall back to ws://).
COPY server/ ./server/
COPY static/ ./static/
COPY blurtd ./blurtd
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The unprivileged runtime user. Created after the heavy pip layers so those
# stay cached, and given a fixed uid so a host-side `chown` on the cache volume
# is reproducible. /app stays root-owned and read-only to the daemon — code
# execution in the decode path shouldn't be able to rewrite the code it runs
# from — which is why the auto-generated cert goes on the cache volume instead
# of into /app/certs, with the entrypoint pointing BLURT_CERT_DIR at it.
#
# /app/certs is still created, empty: it's the documented mount point for your
# own certs (`-v ./certs:/app/certs:ro`), and the entrypoint prefers it when it
# holds a pair. It is deliberately NOT a symlink into the cache — Docker
# resolves a symlinked mount point and mounts at its target, where the cache
# volume then shadows it, so a user's mounted certs are silently ignored.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin blurt \
    && mkdir -p /home/blurt/.cache/blurt-certs /app/certs \
    && chown -R blurt:blurt /home/blurt

ENV HOME=/home/blurt \
    BLURT_CACHE=/home/blurt/.cache \
    HOST=0.0.0.0 \
    PORT=25878 \
    HF_HOME=/home/blurt/.cache/huggingface

# Host-RAM tuning. This is a GPU inference server: the real work runs on the
# device and the process sits near-idle on CPU, yet torch/OpenMP/MKL/numba spin
# up one thread pool per library (~60 threads by default), each carrying a stack
# and its own glibc malloc arena. That inflates resident host RAM for no gain.
#
# MALLOC_ARENA_MAX caps glibc's per-thread heap arenas (default 8 × ncpu). Each
# arena reserves and retains freed memory independently, so on a many-core host a
# heavily-threaded process fragments RAM across dozens of arenas that never
# release back to the OS. Pinning it to 2 concentrates allocations so freed pages
# can actually be trimmed — typically a few hundred MB lower RSS. Must be an env
# var (not set from Python): glibc reads it at libc init, before the interpreter
# starts. The lock contention this trades away is irrelevant here — CPU is idle.
#
# The *_NUM_THREADS caps shrink those thread pools to match the near-zero CPU
# load, cutting both thread-stack memory and pointless context switching. torch
# also re-asserts its intra-op cap in code (see server/asr.py).
ENV MALLOC_ARENA_MAX=2 \
    OMP_NUM_THREADS=2 \
    MKL_NUM_THREADS=2 \
    NUMEXPR_NUM_THREADS=2 \
    NUMBA_NUM_THREADS=2

# 25878: native WebSocket protocol (wss) + the OpenAI-compatible /v1 API.
# 10300: Wyoming (Home Assistant STT).
#
# EXPOSE is documentation only — it neither opens a port nor publishes one, and
# `-p` works regardless of what's listed here. So declaring 10300 costs nothing:
# the listener is still off unless WYOMING_PORT says otherwise, and reaching it
# from the host still needs an explicit `-p 10300:10300`. Both gates stay shut
# by default; this just means `docker inspect` and `docker run -P` know the port
# exists.
EXPOSE 25878 10300

USER blurt

# The entrypoint mints a TLS cert if needed, then runs `python -m server`.
# Append daemon flags as CMD, e.g.  docker run ... blurtd --port 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD []
