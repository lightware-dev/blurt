import AppKit
import AVFoundation
import ApplicationServices

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private var injectItem: NSMenuItem?
    private var hotKey: HotKey?
    // Registered only while recording so it never swallows Esc globally otherwise.
    private var cancelKey: HotKey?

    private let audio = AudioCapture()
    private let client = DictationClient()
    private let hud = HUD()
    private var recording = false
    private var onboarding: OnboardingWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusItem()
        wire()
        setupHotKey()
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
        add(menu, "Start / Stop Blurting  (⌥Space)", #selector(toggle))
        menu.addItem(.separator())
        add(menu, "Setup & Permissions…", #selector(showOnboarding))
        add(menu, "Set Server URL…", #selector(setServer))
        add(menu, "Set Auth Token…", #selector(setToken))
        let inject = NSMenuItem(title: "Insert via Typing (not Paste)",
                                action: #selector(toggleInject), keyEquivalent: "")
        inject.target = self
        inject.state = Settings.injectMode == "type" ? .on : .off
        injectItem = inject
        menu.addItem(inject)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Blurt", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        quit.target = NSApp
        menu.addItem(quit)
        statusItem.menu = menu
    }

    private func add(_ menu: NSMenu, _ title: String, _ sel: Selector) {
        let item = NSMenuItem(title: title, action: sel, keyEquivalent: "")
        item.target = self
        menu.addItem(item)
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
        client.onPartial = { [weak self] text in self?.hud.show(text) }
        client.onFinal = { [weak self] text in
            self?.hud.hide()
            self?.client.close()
            if !text.isEmpty { TextInjector.inject(text) }
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

    private func setupHotKey() {
        hotKey = HotKey(keyCode: Settings.hotKeyCode, modifiers: Settings.hotKeyMods) { [weak self] in
            self?.toggle()
        }
        if hotKey == nil {
            notify("Hotkey unavailable", "Could not register ⌥Space. Another app may own it.")
        }
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

    // MARK: dictation state machine

    @objc private func toggle() {
        recording ? stopRecording() : startRecording()
    }

    private func startRecording() {
        guard !recording else { return }
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

    // MARK: settings actions

    @objc private func setServer() {
        if let v = prompt("Server WebSocket URL", Settings.serverURL,
                          "e.g. wss://192.168.1.50:25878/ws") {
            Settings.serverURL = v.trimmingCharacters(in: .whitespaces)
        }
    }

    @objc private func setToken() {
        if let v = prompt("Auth token (blank = none)", Settings.authToken, "") {
            Settings.authToken = v.trimmingCharacters(in: .whitespaces)
        }
    }

    @objc private func toggleInject() {
        Settings.injectMode = Settings.injectMode == "type" ? "paste" : "type"
        injectItem?.state = Settings.injectMode == "type" ? .on : .off
    }

    // MARK: helpers

    private func prompt(_ title: String, _ value: String, _ placeholder: String) -> String? {
        let alert = NSAlert()
        alert.messageText = title
        alert.addButton(withTitle: "Save")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        field.stringValue = value
        field.placeholderString = placeholder
        alert.accessoryView = field
        NSApp.activate(ignoringOtherApps: true)
        return alert.runModal() == .alertFirstButtonReturn ? field.stringValue : nil
    }

    private func notify(_ title: String, _ body: String) {
        let alert = NSAlert()
        alert.messageText = title
        alert.informativeText = body
        NSApp.activate(ignoringOtherApps: true)
        alert.runModal()
    }
}
