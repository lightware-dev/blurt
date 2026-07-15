#!/usr/bin/env bash
# Regenerates public/og.png (1200x630) from scripts/og.html.
#
# The card is plain HTML/CSS using the site's design tokens (see global.css);
# edit scripts/og.html, re-run this, commit the new PNG.
#
# Renders at 2x with headless Chrome for crisp text, then downscales to
# 1200x630 with sips (macOS) or ImageMagick. Needs network access for the
# Google Fonts the card loads (Space Grotesk, JetBrains Mono).
set -euo pipefail

cd "$(dirname "$0")/.."

find_chrome() {
    local candidates=(
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        "/Applications/Chromium.app/Contents/MacOS/Chromium"
        "$(command -v google-chrome || true)"
        "$(command -v chromium || true)"
        "$(command -v chromium-browser || true)"
    )
    for c in "${candidates[@]}"; do
        if [[ -n "$c" && -x "$c" ]]; then
            echo "$c"
            return 0
        fi
    done
    return 1
}

chrome="$(find_chrome)" || {
    echo "error: no Chrome/Chromium found" >&2
    exit 1
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

"$chrome" \
    --headless=new \
    --disable-gpu \
    --hide-scrollbars \
    --force-device-scale-factor=2 \
    --window-size=1200,630 \
    --virtual-time-budget=8000 \
    --screenshot="$tmp/og-2x.png" \
    "file://$PWD/scripts/og.html" 2>/dev/null

if command -v sips >/dev/null; then
    sips -z 630 1200 "$tmp/og-2x.png" --out public/og.png >/dev/null
elif command -v magick >/dev/null; then
    magick "$tmp/og-2x.png" -resize 1200x630 public/og.png
elif command -v convert >/dev/null; then
    convert "$tmp/og-2x.png" -resize 1200x630 public/og.png
else
    echo "error: need sips or ImageMagick to downscale" >&2
    exit 1
fi

echo "wrote public/og.png ($(wc -c <public/og.png | tr -d ' ') bytes)"
