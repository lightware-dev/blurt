#!/usr/bin/env sh
# blurtd container entrypoint.
#
# Browsers block LAN microphone access over plain ws://, so the server needs a
# TLS cert to be useful off-localhost. To make the image work out of the box we
# auto-generate a self-signed cert on first start (the Mac client trusts it
# outright; browsers prompt once). Provide your own by mounting a cert at
# /app/certs, or disable this entirely with BLURT_AUTOCERT=0.
set -e

# Skip all of this for the help paths — no need to mint a cert.
for arg in "$@"; do
  case "$arg" in
    -h | --help) exec python -m server "$@" ;;
  esac
done

# Where the cert pair lives. Three cases, in priority order:
#
#   1. /app/certs already holds a pair — you mounted your own (typically
#      `-v ./certs:/app/certs:ro`). Use it as-is and never write there.
#   2. /app/certs is writable — you mounted an empty writable dir, so you want
#      the auto-cert to land on your host. Generate into it.
#   3. Otherwise — the image's empty, root-owned /app/certs. The daemon runs
#      unprivileged and can't write to /app, so the auto-cert goes on the cache
#      volume, where it also persists across `docker run` with no extra mount
#      (a stable fingerprint means browsers only prompt once).
#
# The server reads BLURT_CERT_DIR, defaulting to <source root>/certs.
MOUNTED_CERTS=/app/certs
CACHE_CERTS="${BLURT_CACHE:-$HOME/.cache}/blurt-certs"

if [ -f "$MOUNTED_CERTS/cert.pem" ] && [ -f "$MOUNTED_CERTS/key.pem" ]; then
  CERT_DIR="$MOUNTED_CERTS"
elif [ -w "$MOUNTED_CERTS" ]; then
  CERT_DIR="$MOUNTED_CERTS"
else
  CERT_DIR="$CACHE_CERTS"
  mkdir -p "$CERT_DIR"
fi
export BLURT_CERT_DIR="$CERT_DIR"

if [ "${BLURT_AUTOCERT:-1}" != "0" ] && [ ! -f "$CERT_DIR/cert.pem" ]; then
  echo "[blurtd] no cert found — generating a self-signed one for wss:// (BLURT_AUTOCERT=0 to disable)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
    -subj "/CN=blurtd" \
    -addext "subjectAltName=DNS:localhost,DNS:$(hostname),IP:127.0.0.1" 2>/dev/null
  chmod 600 "$CERT_DIR/key.pem"
fi

exec python -m server "$@"
