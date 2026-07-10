# Blurt — Windows tray client

Native .NET 8 / WPF system-tray app that streams your mic to the Parakeet server
and types the transcript into the focused field. No taskbar window; lives in the
notification area. The Windows twin of [`clients/mac`](../mac) — same design,
same server protocol, same behaviour.

## What it does

- **Tray icon** — a mic that turns coral while recording. Right-click for the menu;
  double-click to toggle dictation.
- **Global hotkey** — **Ctrl+Alt+Space** toggles dictation from anywhere. (Windows
  reserves plain Alt+Space for the window system menu, so Blurt can't reuse the
  Mac's ⌥Space.)
- **Live HUD** — a borderless, click-through overlay near the bottom of the screen
  shows partial text as you speak, with a live audio **waveform** (a voice-shaped
  FFT meter, brand yellow) that ripples while listening and swells with your voice.
  On multi-monitor setups it appears on the screen under the **mouse cursor**.
- **Text injection** — on stop, the final transcript is inserted into whatever field
  has focus, either by **paste** (clipboard + Ctrl+V, fast) or **type** (per-character
  Unicode keystrokes, works in terminals). Toggle in the tray menu. Unlike macOS,
  Windows needs **no Accessibility permission** for this.
- **First-run setup** — point Blurt at your server URL + optional auth token, run a
  quick mic test, and optionally start at sign-in.
- **Auto-update** — on launch (and via **Check for Updates…** in the tray menu) Blurt
  asks GitHub for the latest release; if it's newer it offers to download and restart
  into it. See [Updates](#updates). (The macOS client updates through Homebrew instead.)

Settings persist to `%APPDATA%\Blurt\config.json`.

## Architecture

Mirrors the Mac client file-for-file:

| File | Role | Mac twin |
|------|------|----------|
| `Program.cs` | STA entry point, WPF app host | `main.swift` |
| `BlurtApp.cs` | Tray + menu, wiring, dictation state machine | `AppDelegate.swift` |
| `HotKey.cs` | `RegisterHotKey` on a hidden message window | `HotKey.swift` |
| `AudioCapture.cs` | NAudio mic → 16 kHz mono PCM16 frames + FFT bands | `AudioCapture.swift` |
| `Spectrum.cs` | PCM16 → log-spaced FFT band magnitudes | `Spectrum.swift` |
| `DictationClient.cs` | `ClientWebSocket` → `{start}`/PCM/`{stop}` | `DictationClient.swift` |
| `TextInjector.cs` | `SendInput` paste / Unicode type | `TextInjector.swift` |
| `Hud.cs` | Click-through topmost overlay | `HUD.swift` |
| `Waveform.cs` | Animated layered-ribbon audio meter | `Waveform.swift` |
| `Onboarding.cs` | First-run setup window | `Onboarding.swift` |
| `Settings.cs` | JSON-backed config | `Settings.swift` |
| `Brand.cs` | Shared palette + fonts | `Brand.swift` |
| `IconFactory.cs` | Tray icon drawn at runtime (no `.ico` asset) | — |
| `StartupRegistration.cs` | HKCU Run-key "start at login" | — |
| `Updater.cs` | GitHub-release check + self-replace update | — (Mac uses Homebrew) |

The server WebSocket protocol is identical to the Mac client's, including trusting
the server's self-signed LAN certificate.

## Build (on a Windows machine)

Requires the [.NET 8 SDK](https://dotnet.microsoft.com/download). The only NuGet
dependency is **NAudio** (mic capture); everything else is WPF / WinForms / Win32
P/Invoke.

```powershell
cd clients\windows

# Dev iteration — runs against the installed .NET runtime:
dotnet run

# Release — single self-contained Blurt.exe, no runtime needed on the target:
dotnet publish Blurt.csproj -c Release -r win-x64 --self-contained true `
  -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true `
  -p:EnableCompressionInSingleFile=true -o publish
# → publish\Blurt.exe
```

First run: the setup window opens. Enter your server URL (e.g.
`wss://192.168.1.50:25878/ws`), run the mic test, hit **Start Blurting**, then press
**Ctrl+Alt+Space** in any app and talk.

> **No local Windows box?** You don't need one — the CI workflow builds and
> publishes `Blurt.exe` for you (see below). Download the artifact/zip and run it
> on any Windows 10/11 machine.

## Updates

Blurt updates itself from GitHub Releases — no installer, no separate updater
process, no extra dependency (`Updater.cs`).

- **Check.** On launch (and on demand via **Check for Updates…** in the tray menu)
  it calls the GitHub API for the [latest release](https://github.com/lightware-dev/blurt/releases/latest),
  parses its `v*` tag, and compares it to the running build's `<Version>`. The launch
  check is silent unless there's something newer (and is skipped under a debugger).
- **Download.** If you accept the prompt, it downloads the release's
  `Blurt-Windows.zip` to a temp folder and extracts `Blurt.exe`.
- **Apply + restart.** You can't overwrite a *running* exe, but Windows lets you
  *rename* it. So Blurt renames the live `Blurt.exe` → `Blurt.old.exe`, copies the new
  build into place, launches it, and quits. The fresh instance deletes the leftover
  `Blurt.old.exe` on its next startup.

Because the app is a portable single-file exe you run from wherever you unzipped it,
this needs no elevation. If it's installed somewhere write-protected (e.g. under
`Program Files`) the swap is denied gracefully and Blurt points you at the Releases
page to update by hand. The self-replace relies on the release keeping a stable
`Blurt-Windows.zip` asset — which `windows.yml` always attaches.

## CI & releases

`.github/workflows/windows.yml` (runs on `windows-latest`) builds the single-file
release exe and uploads it as a zipped Actions artifact (`Blurt-<version>-Windows.zip`).
Path-filtered to `clients/windows/**`.

**Code signing (optional).** If the repo/org secrets `WINDOWS_CERT_PFX_BASE64` +
`WINDOWS_CERT_PASSWORD` are present (and it isn't a PR build), the exe is
Authenticode-signed with `signtool` and timestamped. Without them the exe ships
unsigned — it still runs, but SmartScreen shows a "Windows protected your PC"
warning on first launch (click **More info ▸ Run anyway**). An OV/EV code-signing
cert removes that warning.

**Cutting a release.** Bump `<Version>` in `Blurt.csproj`, then push a matching
`v*` tag:

```bash
git tag v1.1 && git push origin v1.1
```

The tag run publishes a GitHub Release with `Blurt-<version>-Windows.zip` plus a
stable `Blurt-Windows.zip` for a fixed download URL:

```
https://github.com/lightware-dev/blurt/releases/latest/download/Blurt-Windows.zip
```

Both the macOS and Windows workflows attach to the **same** `v*` release, so keep
`Blurt.csproj`'s `<Version>` and the Mac `Info.plist` version in step when tagging.
```
