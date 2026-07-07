#!/usr/bin/env bash
# Builds, Developer ID-signs, notarizes, and staples Blurt.app, producing a
# distributable zip in clients/mac/dist/ that passes Gatekeeper on any Mac
# (not just this one).
#
# This is the "ship it" path; ./build-app.sh alone is fine for local runs.
#
# Signing identity:
#   SIGN_IDENTITY   Developer ID Application identity (full name or SHA-1).
#                   Defaults to the Lightware Developer ID (see build-app.sh).
#
# Notary credentials — pick ONE:
#   NOTARY_PROFILE  name of a profile saved via `xcrun notarytool store-credentials`
#   — or all three —
#   NOTARY_KEY      path to the App Store Connect API key .p8
#   NOTARY_KEY_ID   the key's Key ID
#   NOTARY_ISSUER   the team's Issuer ID
#
# Usage: ./notarize.sh
set -euo pipefail
cd "$(dirname "$0")"

APP="Blurt.app"

# Assemble notarytool credential args from whichever env vars are provided.
notary_args=()
if [ -n "${NOTARY_PROFILE:-}" ]; then
    notary_args=(--keychain-profile "$NOTARY_PROFILE")
elif [ -n "${NOTARY_KEY:-}" ] && [ -n "${NOTARY_KEY_ID:-}" ] && [ -n "${NOTARY_ISSUER:-}" ]; then
    notary_args=(--key "$NOTARY_KEY" --key-id "$NOTARY_KEY_ID" --issuer "$NOTARY_ISSUER")
else
    echo "error: set NOTARY_PROFILE, or NOTARY_KEY + NOTARY_KEY_ID + NOTARY_ISSUER" >&2
    exit 1
fi

# 1. Build + Developer ID sign (build-app.sh adds hardened runtime + secure
#    timestamp + the mic entitlement). Fail loudly if it falls back to ad-hoc.
./build-app.sh
if codesign -dvv "$APP" 2>&1 | grep -q "Signature=adhoc"; then
    echo "error: $APP is ad-hoc signed — Developer ID cert not found in keychain" >&2
    exit 1
fi

VERSION="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$APP/Contents/Info.plist")"
mkdir -p dist
SUBMIT_ZIP="$(mktemp -d)/notarize.zip"
DIST_ZIP="dist/Blurt-${VERSION}-macOS.zip"

# 2. Notarize: submit a zip of the .app and block until Apple returns a verdict.
#    On failure, dump the detailed log so the rejection reason is visible.
echo "==> notarizing (this can take a few minutes)"
ditto -c -k --keepParent "$APP" "$SUBMIT_ZIP"
if ! submit_out="$(xcrun notarytool submit "$SUBMIT_ZIP" "${notary_args[@]}" --wait 2>&1)"; then
    printf '%s\n' "$submit_out"
    sub_id="$(printf '%s\n' "$submit_out" | awk '/id:/{print $2; exit}')"
    [ -n "$sub_id" ] && xcrun notarytool log "$sub_id" "${notary_args[@]}" || true
    exit 1
fi
printf '%s\n' "$submit_out"

# 3. Staple the notarization ticket onto the .app, then zip the stapled app to ship.
echo "==> stapling"
xcrun stapler staple "$APP"
ditto -c -k --keepParent "$APP" "$DIST_ZIP"

# 4. Verify it will pass Gatekeeper on a clean Mac.
echo "==> verifying"
codesign --verify --deep --strict --verbose=2 "$APP"
xcrun stapler validate "$APP"
spctl -a -vvv -t exec "$APP"

echo "==> done: clients/mac/$DIST_ZIP (notarized + stapled)"
