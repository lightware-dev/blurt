#!/usr/bin/env bash
# Generate a self-signed TLS cert for wss:// on the LAN.
# Certs are gitignored — run this once per machine (or when the host changes).
#
#   ./scripts/gen_certs.sh            # default: this machine, "localhost"
#   ./scripts/gen_certs.sh 192.168.1.5   # or an explicit IP
#   ./scripts/gen_certs.sh myhost        # or an explicit hostname
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CERTS="$ROOT/certs"
mkdir -p "$CERTS"

HOST="${1:-localhost}"

# Build the SAN list: classify the host as an IP or a DNS name, plus loopback.
if [[ "$HOST" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  SAN="IP:$HOST,IP:127.0.0.1,DNS:localhost"
else
  SAN="DNS:$HOST,IP:127.0.0.1,DNS:localhost"
fi

echo "Generating self-signed cert for $HOST (+ 127.0.0.1, localhost) ..."
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$CERTS/key.pem" -out "$CERTS/cert.pem" \
  -subj "/CN=$HOST" \
  -addext "subjectAltName=$SAN"

chmod 600 "$CERTS/key.pem"
printf '%s\n' "$HOST" > "$CERTS/.lanip"

echo "Wrote certs/cert.pem, certs/key.pem (host=$HOST)"
echo "Point the Mac client at: wss://$HOST:7860/ws"
