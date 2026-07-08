#!/usr/bin/env sh
# blurtd container entrypoint.
#
# Browsers block LAN microphone access over plain ws://, so the server needs a
# TLS cert to be useful off-localhost. To make the image work out of the box we
# auto-generate a self-signed cert on first start (the Mac client trusts it
# outright; browsers prompt once). Provide your own by mounting a cert at
# /app/certs, or disable this entirely with BLURT_AUTOCERT=0.
set -e

# Skip all of this for the metadata/help paths — no need to mint a cert.
for arg in "$@"; do
  case "$arg" in
    --list-models | -h | --help) exec python -m server "$@" ;;
  esac
done

CERT_DIR=/app/certs

# If the user didn't mount their own certs/, back the dir with the cache volume
# so the auto-cert persists across `docker run` with no extra mount (a stable
# fingerprint means browsers only prompt once).
if [ ! -d "$CERT_DIR" ]; then
  mkdir -p /root/.cache/blurt-certs
  ln -s /root/.cache/blurt-certs "$CERT_DIR"
fi

if [ "${BLURT_AUTOCERT:-1}" != "0" ] && [ ! -f "$CERT_DIR/cert.pem" ]; then
  echo "[blurtd] no cert found — generating a self-signed one for wss:// (BLURT_AUTOCERT=0 to disable)"
  openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
    -keyout "$CERT_DIR/key.pem" -out "$CERT_DIR/cert.pem" \
    -subj "/CN=blurtd" \
    -addext "subjectAltName=DNS:localhost,DNS:$(hostname),IP:127.0.0.1" 2>/dev/null
  chmod 600 "$CERT_DIR/key.pem"
fi

exec python -m server "$@"
