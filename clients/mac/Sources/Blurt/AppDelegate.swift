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
    // Shown only while the server is known to be down (see setServerReachable).
    private var serverDownItem: NSMenuItem?
    private var serverDownSeparator: NSMenuItem?
    // Registered only while recording so it never swallows Esc globally otherwise.
    private var cancelKey: HotKey?

    // The most recent finalized dictation, kept in memory only so it can be
    // recovered from the menu when it landed somewhere unexpected. Never persisted.
    private var lastDictation: String?

    private let audio = AudioCapture()
    private let client = DictationClient()
    private let hud = HUD()
    private var recording = false
    // True while a certificate pre-flight is in flight, so a second trigger can't
    // stack another probe (and another dialog) on top of the first.
    private var preflighting = false
    // True while an alert is on screen. A global hotkey still fires over a modal,
    // so without this an impatient second press against a server that's down
    // starts a dictation behind the dialog telling you it's down — and stacks a
    // second copy of that dialog when it fails too.
    private var alerting = false
    // Live protocol state for the HUD: the latest structured partial, whether the
    // server's VAD currently hears speech, and whether the model is still loading.
    private var partialCommitted = ""
    private var partialLive = ""
    private var serverHearing = false
    private var serverLoading = false
    // Whether the server has said anything on this connection. Until it has,
    // the HUD must not claim to be listening: the mic is live, but nothing is
    // receiving it.
    private var serverAcked = false
    // The last thing we learned about whether the server answers at all — from
    // a certificate pre-flight or a dictation. Nil until something has tried.
    // Drives the menu bar, so a dead daemon is visible *before* the hotkey is
    // pressed and not only after a dictation has been spoken into the void.
    private var serverReachable: Bool?
    private var onboarding: OnboardingWindowController?
    private var settingsWindow: SettingsWindowController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        setupStatusItem()
        wire()
        applyShortcut()
        // Show the first-run permissions screen until the user has seen it *and*
        // Accessibility is actually granted (without it Blurt can't type anything).
        if !Settings.didOnboard || !AXIsProcessTrusted() {
            showOnboarding()   // the certificate check follows once it's dismissed
        } else {
            AVCaptureDevice.requestAccess(for: .audio) { _ in }
            checkServerCertificate()
        }
    }

    /// Settle trust in the server's TLS certificate now, while nothing is being
    /// dictated, so the first hotkey press isn't what triggers a trust dialog.
    /// Silent unless the certificate is unknown or has changed.
    ///
    /// A global hotkey still fires while a modal alert is up, so this holds
    /// `preflighting` for the duration — otherwise a trigger during the dialog
    /// would start recording behind it.
    private func checkServerCertificate() {
        preflighting = true
        CertTrust.preflight(Settings.serverURL) { [weak self] outcome in
            guard let self else { return }
            self.preflighting = false
            self.noteProbe(outcome)
        }
    }

    /// Fold what a pre-flight learned into the menu bar's picture of the server.
    /// The probe already opens a connection, so it knows whether anything is
    /// listening — dropping that on the floor is why a stopped daemon stayed
    /// invisible until a dictation had already been lost to it.
    private func noteProbe(_ outcome: CertTrust.Outcome) {
        guard CertTrust.probesReachability(Settings.serverURL) else { return }
        setServerReachable(outcome != .unreachable)
    }

    /// Record whether the server answers, and reflect it in the menu bar: a
    /// dimmed icon plus a line naming the host that isn't responding.
    private func setServerReachable(_ reachable: Bool) {
        guard serverReachable != reachable else { return }
        serverReachable = reachable
        updateIcon()
        updateServerDownItem()
    }

    /// `host:port` for the configured server — what to name in a message about
    /// it failing, rather than the whole `wss://…/ws` URL.
    private static func serverLabel() -> String {
        guard let url = URL(string: Settings.serverURL), let host = url.host else {
            return Settings.serverURL
        }
        guard let port = url.port else { return host }
        return "\(host):\(port)"
    }

    // MARK: menu bar

    private func setupStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        updateIcon()

        let menu = NSMenu()
        // Manage the "Copy Last Dictation" enabled state ourselves; otherwise the
        // default auto-enabling would keep it clickable before anything's captured.
        menu.autoenablesItems = false
        // First, above everything, and hidden unless it applies: if the server
        // is down there is nothing else worth reading in here.
        let down = NSMenuItem(title: "", action: nil, keyEquivalent: "")
        down.isEnabled = false
        menu.addItem(down)
        let downSeparator = NSMenuItem.separator()
        menu.addItem(downSeparator)
        serverDownItem = down
        serverDownSeparator = downSeparator
        updateServerDownItem()
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
        // Faded while the server is known to be down — the same "connected or
        // not" idiom every other sync-something-to-a-server menu bar app uses.
        // Never while recording: the coral mark has to stay unambiguous.
        let down = !recording && serverReachable == false
        button.alphaValue = down ? 0.4 : 1
        button.toolTip = down ? "Blurt — can't reach \(Self.serverLabel())" : "Blurt"
    }

    private func updateServerDownItem() {
        let down = serverReachable == false
        serverDownItem?.title = "Can't reach \(Self.serverLabel())"
        serverDownItem?.isHidden = !down
        serverDownSeparator?.isHidden = !down
    }

    // MARK: wiring

    private func wire() {
        audio.onFrame = { [weak self] data in self?.client.sendAudio(data) }
        audio.onSpectrum = { [weak self] bands in self?.hud.spectrum(bands) }
        // The server's first word. Only now is the HUD entitled to say it's
        // listening, and only now do we know the daemon is up.
        client.onConnected = { [weak self] in
            self?.serverAcked = true
            self?.setServerReachable(true)
            self?.renderHUD()
        }
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
            // The placeholder only renders before the first word arrives, so
            // the meter is what carries this state for the rest of the
            // dictation.
            self?.hud.hearing(speech)
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
        client.onUnreachable = { [weak self] detail in
            self?.connectionFailed(detail, unreachable: true)
        }
        client.onError = { [weak self] msg in
            self?.connectionFailed(msg, unreachable: false)
        }
    }

    /// Every way a dictation's connection can die ends here: stop recording,
    /// then say what happened. `unreachable` means the server never spoke at
    /// all, which has one overwhelmingly likely cause worth naming outright.
    private func connectionFailed(_ detail: String, unreachable: Bool) {
        forceStop()
        // A refused certificate arrives here as an opaque TLS failure. Say
        // what really happened and offer to pin it — the fingerprint is
        // already in hand, so this needs no second handshake. Reaching this
        // means the certificate changed since the pre-flight; the next
        // trigger connects.
        if let rejection = client.certRejection {
            hud.hide()
            // Either way this dictation is gone, so say so rather than
            // letting the HUD vanish with no explanation.
            if CertTrust.promptAndPin(rejection.decision, host: rejection.host, port: rejection.port) {
                notify("Certificate trusted", "Press the shortcut again to dictate.")
            } else {
                notify("Connection refused",
                       "The certificate for \(rejection.host) isn't trusted, so Blurt didn't connect.")
            }
            return
        }
        guard unreachable else {
            hud.hide()
            notify("Connection error", detail)
            return
        }
        setServerReachable(false)
        // Put the diagnosis in the HUD before the alert. The HUD is the one
        // surface guaranteed to be in front of the user — it follows them onto
        // full-screen spaces, which an alert from a menu bar agent does not —
        // and it's where they were already looking for their words.
        hud.flash("Can't reach the server", for: 2.5)
        notify("Can't reach the Blurt server",
               """
               Nothing answered at \(Self.serverLabel()), so this dictation was lost. \
               Check that blurtd is running there, then press \
               \(ShortcutLabel.current().isEmpty ? "the shortcut" : ShortcutLabel.current()) again.

               (\(detail))
               """)
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
            // A new server means a new certificate: settle it here rather than
            // letting the next dictation discover it.
            controller.onServerChanged = { [weak self] in self?.checkServerCertificate() }
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
                self?.checkServerCertificate()
            }
            onboarding = controller
        }
        onboarding?.present()
    }

    /// One render path for the live HUD: structured partial when there's text,
    /// otherwise a placeholder that reflects what the server is actually doing.
    private func renderHUD() {
        guard recording else { return }
        // "Connecting…" until the server has actually answered. Saying
        // "Listening…" over a socket nobody is on the other end of is the whole
        // reason a stopped daemon could go unnoticed for a minute at a time.
        let placeholder = !serverAcked  ? "Connecting…"
                        : serverLoading ? "Loading model…"
                        : serverHearing ? "Hearing you…"
                        : "Listening…"
        hud.show(committed: partialCommitted, live: partialLive, placeholder: placeholder)
    }

    // MARK: dictation state machine

    @objc private func toggle() {
        guard !preflighting, !alerting else { return }
        recording ? stopRecording() : startRecording()
    }

    /// Settle the server's certificate *before* the HUD and the mic come up. A
    /// trust dialog over a live "Listening…" HUD would swallow whatever the user
    /// said while it was on screen, so the first dictation against an unknown
    /// server pays a handshake here instead of losing itself. Only runs when the
    /// host is still unsettled — the steady state goes straight to `beginRecording`.
    private func startRecording() {
        guard !recording else { return }
        guard CertTrust.needsCheck(Settings.serverURL) else { beginRecording(); return }
        preflighting = true
        CertTrust.preflight(Settings.serverURL) { [weak self] outcome in
            guard let self else { return }
            self.preflighting = false
            self.noteProbe(outcome)
            // `.unreachable` falls through to a normal connection attempt, so an
            // offline server still gives the usual error rather than silence.
            guard outcome != .refused else { return }
            self.beginRecording()
        }
    }

    private func beginRecording() {
        guard !recording else { return }
        partialCommitted = ""
        partialLive = ""
        serverHearing = false
        serverLoading = false
        serverAcked = false
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
        hud.hearing(false)   // muted until the server reports it hears speech
        renderHUD()          // no text yet → the faded "Connecting…" placeholder
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
        alerting = true
        alert.runModal()
        alerting = false
    }
}
