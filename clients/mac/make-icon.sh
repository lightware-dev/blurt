#!/usr/bin/env bash
# Generate AppIcon.icns from AppIcon.svg (the favicon design at icon scale).
# Requires ImageMagick (`magick`) and iconutil (ships with macOS).
set -euo pipefail
cd "$(dirname "$0")"

SVG="AppIcon.svg"
SET="AppIcon.iconset"
ICNS="AppIcon.icns"

command -v magick >/dev/null 2>&1 || { echo "need ImageMagick (brew install imagemagick)"; exit 1; }

echo "▶ rendering ${SVG} → ${SET}…"
rm -rf "$SET"
mkdir -p "$SET"

# name   px
render() {
    magick -background none -density 1200 "$SVG" -resize "${2}x${2}" "$SET/$1"
}
render icon_16x16.png        16
render icon_16x16@2x.png     32
render icon_32x32.png        32
render icon_32x32@2x.png     64
render icon_128x128.png      128
render icon_128x128@2x.png   256
render icon_256x256.png      256
render icon_256x256@2x.png   512
render icon_512x512.png      512
render icon_512x512@2x.png   1024

echo "▶ compiling ${ICNS}…"
iconutil -c icns -o "$ICNS" "$SET"
rm -rf "$SET"
echo "✓ wrote ${ICNS}"
