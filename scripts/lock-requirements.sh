#!/usr/bin/env bash
# Regenerate the pinned, hashed requirements.txt from requirements.in.
#
#   ./scripts/lock-requirements.sh
#
# Needs `uv` (https://docs.astral.sh/uv/). pip-tools works too — the equivalent
# flags are --generate-hashes and --unsafe-package for each exclusion.
set -euo pipefail
cd "$(dirname "$0")/.."

# The oldest interpreter we support, NOT whichever one you are running.
# Compiled on a newer one, the resolution pins wheels the older one cannot
# install at all — the failure shows up only on the oldest CI matrix entry.
PYTHON_VERSION=3.11

# torch comes from the PyTorch cu130 index in the Dockerfile, before this file
# is installed, because those wheels bundle the CUDA + cuDNN runtime. Installing
# any of these from PyPI would clobber that build with a generic one, so they
# are resolved (so everything else is pinned consistently against the torch that
# will actually be present) and then left out of the output.
#
# torchaudio and torchmetrics are deliberately NOT here: they are ordinary PyPI
# installs that the cu130 step does not provide, so they belong in the lock.
EXCLUDE=(
    torch
    triton
    nvidia-cublas
    nvidia-cuda-cupti
    nvidia-cuda-nvrtc
    nvidia-cuda-runtime
    nvidia-cudnn-cu13
    nvidia-cufft
    nvidia-cufile
    nvidia-curand
    nvidia-cusolver
    nvidia-cusparse
    nvidia-cusparselt-cu13
    nvidia-nccl-cu13
    nvidia-nvjitlink
    nvidia-nvshmem-cu13
    nvidia-nvtx
)

args=()
for pkg in "${EXCLUDE[@]}"; do
    args+=(--no-emit-package "$pkg")
done

uv pip compile requirements.in \
    --generate-hashes \
    --universal \
    --python-version "$PYTHON_VERSION" \
    "${args[@]}" \
    -o requirements.txt

echo
echo "Wrote requirements.txt ($(grep -c '^[a-z0-9]' requirements.txt) packages pinned)."
echo "Sanity check — none of the cu130-provided packages should appear:"
# Checked against EXCLUDE itself rather than a blanket `^nvidia-` match: not every
# nvidia-* package comes from the cu130 index. nvidia-modelopt (4-bit weights) and
# its nvidia-ml-py dependency are ordinary PyPI wheels that belong in the lock, and
# a prefix match would reject them. Each entry still matches its -cuXX variants,
# which is how `nvidia-cublas` catches `nvidia-cublas-cu13` — but the suffix has to
# start with a hyphen, or `torch` would also match `torchaudio`, which belongs here.
leaked=()
for pkg in "${EXCLUDE[@]}"; do
    if grep -qE "^${pkg}(-[a-z0-9]+)*==" requirements.txt; then
        leaked+=("$pkg")
    fi
done
if (( ${#leaked[@]} )); then
    echo "  FAIL: cu130-provided package(s) leaked into the lock: ${leaked[*]}" >&2
    exit 1
fi
echo "  ok"
