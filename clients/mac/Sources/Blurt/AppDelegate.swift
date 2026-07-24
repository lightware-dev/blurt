import AppKit
import AVFoundation
import ApplicationServices
import Carbon

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var hotKey: HotKey?
    private var doubleTap: ModifierDoubleTap?
    private var toggleItem: NSMenuItem?
    private var copyLastItem: NSMenuItem?
    // Registered only while recording so it never swallows Esc globally otherwise.
    private var cancelKey: HotKey?

    // The most recent finalized dictation, kept in memory only so it can be
    // recovered from the menu when it landed somewhere unexpected. Never persisted.
    private var lastDictation: String?

    private let audio = AudioCapture()
    private let client = DictationClient()
    private let hud = HUD()
    private var recording = false
    // Live protocol state for the HUD: the latest structured partial, whether the
    // server's VAD currently hears speech, and whether the model is still loading.
    private var partialCommitted = ""
    private var partialLive = ""
    private var serverHearing = false
    private var serverLoading = false
    private var onboarding: OnboardingWindowController?
    private var settingsWindow: SettingsWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusItem()
        wire()
        applyShortcut()
        // Show the first-run permissions screen until the user has seen it *and*
        // Accessibility is actually granted (without it Blurt can't type anything).
        if !Settings.didOnboard || !AXIsProcessTrusted() {
            showOnboarding()
        } else {
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
        }
    }

    // MARK: menu bar

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        updateIcon()

        let menu = NSMenu()
        // Manage the "Copy Last Dictation" enabled state ourselves; otherwise the
        // default auto-enabling would keep it clickable before anything's captured.
        menu.autoenablesItems = false
        toggleItem = add(menu, "Start / Stop Blurting", #selector(toggle))
        copyLastItem = add(menu, "Copy Last Dictation", #selector(copyLastDictation))
        copyLastItem?.isEnabled = false
        menu.addItem(.separator())
        add(menu, "Settings…", #selector(showSettings))
        add(menu, "Setup & Permissions…", #selector(showOnboarding))
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Blurt", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApp
        menu.addItem(quit)
        statusItem.menu = menu
    }

    @discardableResult
    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
        return item
    }

    private func updateIcon() {
        guard let button = statusItem.button else { return }
        // Blurt's own highlighter-bar mark (from the favicon), drawn coral while
        // recording so the menu bar shows "blurting" at a glance.
        button.image = Brand.menuBarIcon(tint: recording ? Brand.coral : nil)
        button.contentTintColor = nil
    }

    // MARK: wiring

    private func wire() {
        audio.onFrame = { [weak self] data in self?.client.sendAudio(data) }
        audio.onSpectrum = { [weak self] bands in self?.hud.spectrum(bands) }
        client.onPartial = { [weak self] committed, live in
            self?.partialCommitted = committed
            self?.partialLive = live
            self?.serverLoading = false   // text proves the model is up
            self?.renderHUD()
        }
        // Server-side VAD: before any text arrives, flip the placeholder to
        // "Hearing you…" — end-to-end confirmation that mic → network → server
        // is alive (the waveform only proves the local mic works).
        client.onVad = { [weak self] speech in
            self?.serverHearing = speech
            // `info` is a one-shot snapshot taken at connect; if it said
            // "loading", nothing else would ever clear it and the placeholder
            // would outrank "Hearing you…" for the whole dictation. Any event
            // from the pipeline proves loading is done.
            self?.serverLoading = false
            self?.renderHUD()
        }
        client.onInfo = { [weak self] state, _ in
            self?.serverLoading = (state == "loading")
            self?.renderHUD()
        }
        client.onFinal = { [weak self] text in
            self?.hud.hide()
            self?.client.close()
            if !text.isEmpty {
                self?.lastDictation = text
                self?.copyLastItem?.isEnabled = true
                TextInjector.inject(text)
            }
        }
        client.onStatus = { [weak self] state, detail in
            // The server reports a fatal decode failure (e.g. a wedged CUDA context)
            // as {status: error}. Surface it instead of leaving the HUD stuck on
            // "Listening…" with no text ever arriving.
            guard state == "error" else { return }
            self?.forceStop()
            self?.hud.hide()
            let first = detail?.split(separator: "\n").first.map(String.init)
            self?.notify("Dictation server error", first ?? "The server could not transcribe. Try again, and restart the server if it persists.")
        }
        client.onError = { [weak self] msg in
            self?.hud.hide()
            self?.forceStop()
            self?.notify("Connection error", msg)
        }
    }

    /// (Re)arm the dictation trigger from Settings.shortcutMode, and reflect the
    /// active shortcut in the menu item title.
    private func applyShortcut() {
        hotKey = nil
        doubleTap = nil
        let fire: () -> Void = { [weak self] in self?.toggle() }
        switch Settings.shortcutMode {
        case .doubleTap:
            doubleTap = ModifierDoubleTap(onFire: fire)
        case .optionSpace:
            hotKey = HotKey(keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey), onFire: fire)
        case .custom:
            hotKey = HotKey(keyCode: Settings.hotKeyCode, modifiers: Settings.hotKeyMods, onFire: fire)
        case .off:
            break
        }
        if hotKey == nil, Settings.shortcutMode == .optionSpace || Settings.shortcutMode == .custom {
            notify("Hotkey unavailable",
                   "Could not register \(ShortcutLabel.current()). Another app may own it.")
        }
        let shortcut = ShortcutLabel.current()
        toggleItem?.title = shortcut.isEmpty
            ? "Start / Stop Blurting"
            : "Start / Stop Blurting  (\(shortcut))"
    }

    /// Copy the last finalized dictation to the clipboard — a safety net for when
    /// the text got injected into the wrong field. Held in memory only.
    @objc private func copyLastDictation() {
        guard let text = lastDictation, !text.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
    }

    @objc private func showSettings() {
        if settingsWindow == nil {
            let controller = SettingsWindowController()
            controller.onChange = { [weak self] in self?.applyShortcut() }
            controller.onCaptureActive = { [weak self] capturing in
                // Suspend triggers while recording a combo so the active hotkey
                // can't swallow the keys being typed into the capture well.
                if capturing {
                    self?.hotKey = nil
                    self?.doubleTap = nil
                } else {
                    self?.applyShortcut()
                }
            }
            settingsWindow = controller
        }
        settingsWindow?.present()
    }

    @objc private func showOnboarding() {
        if onboarding == nil {
            let controller = OnboardingWindowController()
            controller.onClose = { [weak self] in
                Settings.didOnboard = true
                self?.onboarding = nil
            }
            onboarding = controller
        }
        onboarding?.present()
    }

    /// One render path for the live HUD: structured partial when there's text,
    /// otherwise a placeholder that reflects what the server is actually doing.
    private func renderHUD() {
        guard recording else { return }
        let placeholder = serverLoading ? "Loading model…"
                        : serverHearing ? "Hearing you…"
                        : "Listening…"
        hud.show(committed: partialCommitted, live: partialLive, placeholder: placeholder)
    }

    // MARK: dictation state machine

    @objc private func toggle() {
        recording ? stopRecording() : startRecording()
    }

    private func startRecording() {
        guard !recording else { return }
        partialCommitted = ""
        partialLive = ""
        serverHearing = false
        serverLoading = false
        client.connectAndStart()
        do {
            try audio.start()
        } catch {
            client.close()
            notify("Microphone error", error.localizedDescription)
            return
        }
        recording = true
        updateIcon()
        // Esc discards the in-flight dictation. Registered per-session so we only
        // capture Esc while actually listening. keyCode 53 = Escape, no modifiers.
        cancelKey = HotKey(keyCode: 53, modifiers: 0, id: 2) { [weak self] in self?.cancel() }
        hud.show("")   // empty → HUD shows its faded "Listening…" placeholder
    }

    private func stopRecording() {
        guard recording else { return }
        recording = false
        cancelKey = nil
        updateIcon()
        audio.stop()
        client.stop()          // server replies with {final}; onFinal injects + closes
    }

    /// Discard the in-flight dictation: stop without asking the server to finalize,
    /// so nothing gets injected. Bound to Esc.
    private func cancel() {
        guard recording else { return }
        forceStop()
        hud.flash("Cancelled")
    }

    private func forceStop() {
        recording = false
        cancelKey = nil
        updateIcon()
        audio.stop()
        client.close()
    }

    // MARK: helpers

    private func notify(_ title: String, _ body: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}
