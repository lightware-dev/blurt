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

# App icon (Blurt favicon design). Regenerate from AppIcon.svg when possible;
# otherwise reuse the committed AppIcon.icns.
if [ ! -f AppIcon.icns ] && command -v magick >/dev/null 2>&1; then
    ./make-icon.sh
fi
[ -f AppIcon.icns ] && cp AppIcon.icns "$APP/Contents/Resources/AppIcon.icns"

# Sign with Developer ID (for distribution/notarization) when the cert is
# present; otherwise fall back to an ad-hoc signature for local personal use.
# Override the identity with SIGN_IDENTITY=... ./build-app.sh
SIGN_IDENTITY="${SIGN_IDENTITY:-Developer ID Application: Lightware Consulting, Lda (42WB54FVW9)}"
if security find-identity -v -p codesigning | grep -qF "$SIGN_IDENTITY"; then
    echo "▶ signing with Developer ID + hardened runtime…"
    codesign --force --options runtime --timestamp \
        --entitlements Blurt.entitlements \
        --sign "$SIGN_IDENTITY" "$APP"
else
    echo "▶ Developer ID cert not found — ad-hoc signing (personal use)…"
    codesign --force --deep --sign - "$APP"
fi

echo "✓ done"
lipo -info "$APP/Contents/MacOS/$BIN"
echo "Launch with:  open $APP"
echo "First run: grant Microphone, then add Blurt under"
echo "System Settings ▸ Privacy & Security ▸ Accessibility (needed to insert text)."
