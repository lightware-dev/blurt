# Blurt — macOS menu-bar client

Native Swift menu-bar app that streams your mic to the Parakeet server and types
the transcript into the focused field. No Dock icon; lives in the menu bar.

## Build (on the Mac)

Requires the Xcode command-line tools (`xcode-select --install`). No third-party
dependencies — pure AppKit / AVFoundation / Carbon.

```bash
./build-app.sh          # builds a universal (arm64 + x86_64) Blurt.app
open Blurt.app
```

`build-app.sh` compiles with `swift build -c release --arch arm64 --arch x86_64`,
bundles the binary with `Info.plist`, and signs it. If the **Developer ID
Application** cert (Lightware) is in the keychain it signs with that plus a
hardened runtime, secure timestamp, and the mic entitlement; otherwise it falls
back to an ad-hoc signature for local use. `lipo -info` at the end confirms both
architectures.

For dev iteration you can also just `swift run` (single-arch, current machine).

## Notarized distribution

For a build that runs on **any** Mac (not just this one) it must be Developer
ID-signed, notarized, and stapled. `notarize.sh` does the whole chain — build →
sign → submit to Apple → staple → verify — and drops a distributable zip in
`dist/`.

```bash
# One-time: save notary credentials (App Store Connect API key) to the keychain.
xcrun notarytool store-credentials blurt-notary \
  --key /path/AuthKey_XXXX.p8 --key-id <KEY_ID> --issuer <ISSUER_ID>

# Then, any time:
NOTARY_PROFILE=blurt-notary ./notarize.sh   # → dist/Blurt-<version>.zip
```

The App Store Connect key is team-wide (Lightware, `42WB54FVW9`), and the `.p8`
can't be re-downloaded — keep it in a password manager. Credentials can be a
saved `NOTARY_PROFILE`, or `NOTARY_KEY` + `NOTARY_KEY_ID` + `NOTARY_ISSUER`.
Override the signing identity with `SIGN_IDENTITY=...`.

## CI & releases

`.github/workflows/mac.yml` (runs on `macos-15`) builds the release `.app` and
uploads it as a zipped Actions artifact. On pushes to `main` and manual dispatch
it produces a **signed + notarized + stapled** build; PRs and forks fall back to
an ad-hoc signature (so it still verifies it compiles). Path-filtered to
`clients/mac/**`.

**Cutting a release.** Bump `CFBundleShortVersionString` in `Info.plist`, then
push a matching `v*` tag:

```bash
git tag v1.1 && git push origin v1.1
```

The tag build signs + notarizes, then publishes a **GitHub Release** with two
assets: the versioned `Blurt-<version>.zip` and a stable-named `Blurt-macOS.zip`.
The website links the fixed URL:

```
https://github.com/lightware-dev/blurt/releases/latest/download/Blurt-macOS.zip
```

The workflow refuses to publish if the tag doesn't match the bundle version, or
if the signing secrets are missing (never ships an un-notarized release).

**Repo configuration** (Settings ▸ Secrets and variables ▸ Actions):

Secrets (sensitive):
- `DEVID_CERT_P12_BASE64` — base64 of the exported Developer ID `.p12`
- `DEVID_CERT_PASSWORD` — the `.p12` export password
- `KEYCHAIN_PASSWORD` — throwaway password for the temporary CI keychain
- `NOTARY_KEY_P8_BASE64` — base64 of the App Store Connect API key `.p8`

Variables (non-sensitive identifiers — useless without the `.p8`):
- `NOTARY_KEY_ID`, `NOTARY_ISSUER_ID`

Export the `.p12` from **Keychain Access ▸ your Developer ID Application cert ▸
Export**, then `base64 -i cert.p12 | pbcopy` to get the secret value (same for
the `.p8`).

## First run

1. **Microphone** — macOS prompts on first dictation; allow it.
2. **Accessibility** — required to insert text into other apps. The app triggers
   the prompt; open **System Settings ▸ Privacy & Security ▸ Accessibility** and
   enable **Blurt** (toggle off/on if you rebuild).
3. Click the menu-bar **mic** icon ▸ **Set Server URL…** →
   `wss://<your-linux-ip>:25878/ws` (and **Set Auth Token…** if the server has
   `AUTH_TOKEN` set).

## Use

- **Double-tap ⌥** (Option twice, the default) to start; a HUD shows live partials
  as you speak. Double-tap again to stop — the final text is inserted at the cursor.
- Prefer a held chord? Pick **⌥Space** or a custom shortcut in **Settings…**.
- Menu options: **Set Server URL**, **Set Auth Token**, **Insert via Typing (not
  Paste)**, **Quit**.

**Multiple monitors.** The HUD appears on the screen under the **mouse cursor**,
not always the primary display (`HUD.activeScreen()` — `NSScreen.main` tracks the
key window, which for a background app is always the primary). Text insertion is
independent of the screen: synthetic key events go to whatever field holds
**keyboard focus**, wherever it is. These are usually the same screen but can
differ if the mouse and the focused field are on different displays.

## Text insertion

- **Paste (default):** copies the text, sends ⌘V, restores your previous clipboard.
  Fast, works in browsers, Slack, Notes, etc.
- **Typing:** emits each character as a Unicode key event. Slower but works in
  terminals and apps that ignore synthetic ⌘V. Toggle it in the menu.

## Notes / customization

- **Hotkey** defaults to **double-tap ⌥** (`Settings.shortcutMode = .doubleTap`).
  Alternatives — ⌥Space or a custom Carbon chord (`hotKeyCode` / `hotKeyMods`) —
  are selectable in **Settings…**; all persist in UserDefaults.
- **Self-signed cert:** the client trusts the server's TLS cert (LAN use). For a
  properly-signed cert, remove the trust-all block in `DictationClient.swift`.
- **Architecture:** `HotKey` (Carbon global hotkey) · `AudioCapture`
  (AVAudioEngine → 16 kHz PCM16) · `DictationClient` (URLSession WebSocket) ·
  `HUD` (borderless overlay) · `TextInjector` (paste / type) · `AppDelegate`
  (menu + state machine).
```
