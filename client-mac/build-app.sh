#!/usr/bin/env bash
# Build a universal (arm64 + x86_64) Blurt.app. Run this on the Mac.
#
#   ./build-app.sh      # build both arches, bundle, ad-hoc sign
#   open Blurt.app      # launch (grant Mic + Accessibility when prompted)
set -euo pipefail
cd "$(dirname "$0")"

APP="Blurt.app"
BIN="Blurt"

echo "▶ building universal binary (arm64 + x86_64)…"
swift build -c release --arch arm64 --arch x86_64
BINDIR="$(swift build -c release --arch arm64 --arch x86_64 --show-bin-path)"
BUILT="$BINDIR/$BIN"
[ -f "$BUILT" ] || { echo "build product not found at $BUILT"; exit 1; }

echo "▶ assembling ${APP}…"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILT" "$APP/Contents/MacOS/$BIN"
cp Info.plist "$APP/Contents/Info.plist"

echo "▶ ad-hoc signing (personal use)…"
codesign --force --deep --sign - "$APP"

echo "✓ done"
lipo -info "$APP/Contents/MacOS/$BIN"
echo "Launch with:  open $APP"
echo "First run: grant Microphone, then add Blurt under"
echo "System Settings ▸ Privacy & Security ▸ Accessibility (needed to insert text)."
