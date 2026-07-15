# blurtd — the Blurt (Parakeet) dictation daemon, containerized.
#
# GPU image. The torch cu130 wheels bundle the CUDA + cuDNN runtime, so a plain
# python base is enough — the NVIDIA Container Toolkit injects the host driver at
# run time. Requires `--gpus all` (driver must support CUDA 13; 5090 → 570+).
#
#   docker build -t blurtd .
#   docker run --gpus all -p 25878:25878 -v blurt-cache:/root/.cache blurtd
#
# Models are pulled from HuggingFace on first run into /root/.cache — mount a
# volume there (as above) so you don't re-download on every container start.
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
# satisfied. cu130 wheels match the RTX 5090 (sm_120).
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

ENV HOST=0.0.0.0 \
    PORT=25878 \
    HF_HOME=/root/.cache/huggingface

EXPOSE 25878

# The entrypoint mints a TLS cert if needed, then runs `python -m server`.
# Append daemon flags as CMD, e.g.  docker run ... blurtd --port 8000
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD []
