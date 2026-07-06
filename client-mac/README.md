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
bundles the binary with `Info.plist`, and ad-hoc-signs it. `lipo -info` at the end
confirms both architectures.

For dev iteration you can also just `swift run` (single-arch, current machine).

## First run

1. **Microphone** — macOS prompts on first dictation; allow it.
2. **Accessibility** — required to insert text into other apps. The app triggers
   the prompt; open **System Settings ▸ Privacy & Security ▸ Accessibility** and
   enable **Blurt** (toggle off/on if you rebuild).
3. Click the menu-bar **mic** icon ▸ **Set Server URL…** →
   `wss://<your-linux-ip>:7860/ws` (and **Set Auth Token…** if the server has
   `AUTH_TOKEN` set).

## Use

- Press **⌥Space** (Option-Space) to start; a HUD shows live partials as you speak.
- Press **⌥Space** again to stop — the final text is inserted at the cursor.
- Menu options: **Set Server URL**, **Set Auth Token**, **Insert via Typing (not
  Paste)**, **Quit**.

## Text insertion

- **Paste (default):** copies the text, sends ⌘V, restores your previous clipboard.
  Fast, works in browsers, Slack, Notes, etc.
- **Typing:** emits each character as a Unicode key event. Slower but works in
  terminals and apps that ignore synthetic ⌘V. Toggle it in the menu.

## Notes / customization

- **Hotkey** defaults to ⌥Space (`Settings.hotKeyCode` / `hotKeyMods`, persisted in
  UserDefaults). To change it, adjust those values (Carbon key code + modifier
  mask) — a preferences picker is an easy future addition.
- **Self-signed cert:** the client trusts the server's TLS cert (LAN use). For a
  properly-signed cert, remove the trust-all block in `DictationClient.swift`.
- **Architecture:** `HotKey` (Carbon global hotkey) · `AudioCapture`
  (AVAudioEngine → 16 kHz PCM16) · `DictationClient` (URLSession WebSocket) ·
  `HUD` (borderless overlay) · `TextInjector` (paste / type) · `AppDelegate`
  (menu + state machine).
```
