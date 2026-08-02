import AppKit

/// A small borderless, click-through overlay near the bottom of the screen that
/// shows live partial transcription while dictating. Styled to match the website's
/// HUD (see `www` — the animated demo box): near-black ink pill, the live flowing
/// waveform in brand yellow on the left, and the live text in mono to its right.
final class HUD {
    private var window: NSWindow?
    private let label = NSTextField(labelWithString: "")
    private let wave = WaveformView()
    // The label normally starts right of the waveform; during a flash message
    // the wave is hidden, so the label slides to the pill's leading edge.
    private var labelAfterWave: NSLayoutConstraint?
    private var labelAtLeading: NSLayoutConstraint?

    private let size = NSSize(width: 560, height: 76)
    // Bumped on every show/hide so a scheduled flash auto-hide only fires if no
    // newer session has taken over the HUD in the meantime.
    private var generation = 0
    private var screenObserver: Any?

    /// What the pill is displaying right now. Kept so the window can be rebuilt and
    /// re-shown with identical content if it ever fails to reach the active space.
    private enum Render {
        case partial(committed: String, live: String, placeholder: String)
        case flash(String)
    }
    private var lastRender: Render?

    init() {
        // The window is built once and cached for the app's lifetime. If the display
        // topology changes underneath it (sleep/wake, a monitor un/replugged, a
        // resolution change), its screen association goes stale and it can end up
        // ordered front but positioned off every visible screen — recording runs
        // with an invisible HUD. Drop the cached window on any screen change so the
        // next show() rebuilds it fresh on the current display.
        screenObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main) { [weak self] _ in
            guard let self, let w = self.window else { return }
            w.orderOut(nil)
            self.window = nil
        }
    }

    deinit {
        if let screenObserver { NotificationCenter.default.removeObserver(screenObserver) }
    }

    func show(_ text: String) { show(committed: text, live: "") }

    /// Render a structured partial: the live segment at full strength, settled
    /// text receding behind it (see styledPartial). With no text yet,
    /// `placeholder` is shown faded ("Listening…", or "Hearing you…" once the
    /// server's VAD confirms speech).
    func show(committed: String, live: String, placeholder: String = "Listening…") {
        DispatchQueue.main.async {
            self.present(.partial(committed: committed, live: live, placeholder: placeholder))
        }
    }

    /// The live segment — the words being spoken right now — is what the user
    /// is actually reading, so it keeps the full-strength treatment the HUD has
    /// always used. Settled text behind it recedes to regular weight. Doing it
    /// the other way round would dim the whole pill for a typical dictation,
    /// since text only becomes "committed" after a pause.
    /// Keeps head-truncation so the newest words stay visible.
    private static func styledPartial(committed: String, live: String) -> NSAttributedString {
        let para = NSMutableParagraphStyle()
        para.lineBreakMode = .byTruncatingHead
        let out = NSMutableAttributedString()
        if !committed.isEmpty {
            out.append(NSAttributedString(string: committed, attributes: [
                .font: Brand.mono(18), .foregroundColor: Brand.bone,
                .paragraphStyle: para,
            ]))
        }
        if !live.isEmpty {
            out.append(NSAttributedString(string: committed.isEmpty ? live : " " + live, attributes: [
                .font: Brand.mono(18, .medium), .foregroundColor: Brand.bone,
                .paragraphStyle: para,
            ]))
        }
        return out
    }

    /// Feed the latest FFT frequency-band magnitudes into the spectrum meter.
    func spectrum(_ bands: [Float]) {
        DispatchQueue.main.async { self.wave.setBands(bands) }
    }

    /// Reflect the server's voice-activity state: the meter runs at full
    /// strength while the server reports speech and mutes when it doesn't, so
    /// the pill shows the whole audio path is alive — not just the local mic.
    func hearing(_ on: Bool) {
        DispatchQueue.main.async { self.wave.setHearing(on) }
    }

    func hide() {
        DispatchQueue.main.async {
            self.generation += 1
            self.wave.reset()
            self.window?.orderOut(nil)
        }
    }

    /// Briefly show a dimmed status message (e.g. "Cancelled"), then auto-hide —
    /// unless a newer session has since taken over the HUD.
    func flash(_ text: String) {
        DispatchQueue.main.async {
            let token = self.present(.flash(text))
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                if self.generation == token { self.window?.orderOut(nil) }
            }
        }
    }

    /// Put the pill on screen with the given content, and return the generation
    /// token for this show so a caller can schedule a follow-up (the flash
    /// auto-hide) that no-ops if a newer session has taken the HUD over.
    @discardableResult
    private func present(_ render: Render) -> Int {
        generation += 1
        let token = generation
        lastRender = render
        if window == nil { build() }
        apply(render)
        position()
        orderFront()
        verifyReachedActiveSpace(token: token)
        return token
    }

    private func apply(_ render: Render) {
        switch render {
        case let .partial(committed, live, placeholder):
            if committed.isEmpty && live.isEmpty {
                label.stringValue = placeholder
                label.font = Brand.mono(17)
                label.textColor = Brand.boneDim
            } else {
                label.attributedStringValue = Self.styledPartial(committed: committed, live: live)
            }
            wave.isHidden = false
            labelAtLeading?.isActive = false
            labelAfterWave?.isActive = true
        case let .flash(text):
            label.stringValue = text
            label.font = Brand.mono(17)
            label.textColor = Brand.boneDim
            // Recording is over during a flash message — no meter; the label
            // takes over the wave's spot so the text isn't oddly indented.
            wave.reset()
            wave.isHidden = true
            labelAfterWave?.isActive = false
            labelAtLeading?.isActive = true
        }
    }

    /// Set the two properties that decide *where* the pill lands — which space it
    /// joins, and whether it wins the z-order fight once there — then order it in.
    /// These live here, not in build(), because the panel is cached for the app's
    /// lifetime: setting them at build time means setting them once and then asking
    /// them to survive every space switch, full-screen transition and display sleep
    /// for the rest of the session. A cached window ordered out and back in across
    /// those can come back bound to the space it was last shown on rather than
    /// joining the active one — recording then runs with the pill sitting on a space
    /// the user isn't looking at, which is exactly what a full-screen app on its own
    /// space triggers. Re-applying them per show costs nothing and keeps the window
    /// server's idea of the pill's space membership in sync with ours.
    private func orderFront() {
        guard let w = window else { return }
        // Sit above .statusBar (25): some full-screen apps paint their own content at
        // or above that level and win the z-order fight, leaving the pill present but
        // drawn underneath. .screenSaver (1000) clears them.
        w.level = .screenSaver
        // .canJoinAllApplications is the flag Apple documents for this ("allowing it
        // to join other apps' sets and full screen spaces"). Notably absent:
        // .fullScreenAuxiliary, which reads like the right flag and isn't — it's for a
        // window accompanying its *own* app's full-screen window, and it binds the
        // panel to that window's space. When the app owning that space quits, the
        // binding rots and the pill starts coming back on a stale space instead of the
        // active one, which is why the HUD would strand itself behind a full-screen
        // app after having worked minutes earlier.
        w.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle, .canJoinAllApplications]
        w.orderFrontRegardless()
    }

    /// Confirm the pill actually reached the space the user is looking at.
    /// `orderFrontRegardless()` reports nothing back, so ask the window server:
    /// a window that joined the active space appears in the on-screen-only window
    /// list, and one stranded on another space does not. If it missed, the cached
    /// panel is in a state we can't otherwise detect — throw it away, rebuild it
    /// and replay the same content once. Re-showing goes through `orderFront()`
    /// but deliberately not through this check again, so a genuinely stuck window
    /// can't spin.
    private func verifyReachedActiveSpace(token: Int) {
        guard let number = window?.windowNumber else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15) {
            // A newer show, or a hide, has taken the HUD over in the meantime.
            guard self.generation == token, let render = self.lastRender else { return }
            let onScreen = (CGWindowListCopyWindowInfo([.optionOnScreenOnly], kCGNullWindowID)
                as? [[String: Any]]) ?? []
            let arrived = onScreen.contains { ($0[kCGWindowNumber as String] as? Int) == number }
            guard !arrived else { return }
            NSLog("[Blurt] HUD window \(number) missed the active space; rebuilding")
            self.window?.orderOut(nil)
            self.window = nil
            self.build()
            self.apply(render)
            self.position()
            self.orderFront()
        }
    }

    private func build() {
        // A non-activating NSPanel — not a plain NSWindow — so that ordering it front
        // over a full-screen app never steals focus or kicks that app out of
        // fullscreen. Getting the pill *into* that space is orderFront()'s job.
        let w = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                        styleMask: [.borderless, .nonactivatingPanel],
                        backing: .buffered, defer: false)
        w.isFloatingPanel = true
        w.hidesOnDeactivate = false
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.ignoresMouseEvents = true
        // Level and collection behaviour — which space the pill joins, and whether it
        // wins the z-order fight there — are set in orderFront() instead, so they're
        // re-applied on every show rather than only on the build that happens once.

        let container = NSView()
        container.wantsLayer = true
        container.layer?.backgroundColor = Brand.ink900.withAlphaComponent(0.85).cgColor
        container.layer?.cornerRadius = 18
        container.layer?.borderWidth = 1
        container.layer?.borderColor = Brand.ink700.cgColor
        container.layer?.masksToBounds = true

        // The waveform doubles as the rec indicator: it ripples while listening
        // and swells with the voice, so there's no separate rec dot.
        wave.translatesAutoresizingMaskIntoConstraints = false
        wave.setContentCompressionResistancePriority(.required, for: .horizontal)
        wave.setContentHuggingPriority(.required, for: .horizontal)
        container.addSubview(wave)

        label.font = Brand.mono(18, .medium)
        label.textColor = Brand.bone
        // Single line, fixed width: truncate the *head* so the most recent words
        // stay visible on the right and older text scrolls off the left with an "…".
        label.maximumNumberOfLines = 1
        label.lineBreakMode = .byTruncatingHead
        label.usesSingleLineMode = true
        label.cell?.truncatesLastVisibleLine = true
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        label.setContentHuggingPriority(.defaultLow, for: .horizontal)
        label.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(label)

        labelAfterWave = label.leadingAnchor.constraint(equalTo: wave.trailingAnchor, constant: 12)
        labelAtLeading = label.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 22)
        NSLayoutConstraint.activate([
            wave.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 18),
            wave.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            wave.widthAnchor.constraint(equalToConstant: 96),
            wave.heightAnchor.constraint(equalToConstant: 34),

            labelAfterWave!,
            label.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -22),
            label.centerYAnchor.constraint(equalTo: container.centerYAnchor),
        ])
        w.contentView = container
        window = w
    }

    private func position() {
        guard let w = window, let screen = Self.activeScreen() else { return }
        var x = screen.frame.midX - size.width / 2
        var y = screen.frame.minY + 140
        // Guard against a stale screen frame (e.g. one that lingers momentarily
        // after a display is unplugged) placing the pill where no display can show
        // it. If the computed rect isn't inside any connected screen, fall back to
        // the primary display so the HUD is always visible somewhere.
        let rect = NSRect(origin: NSPoint(x: x, y: y), size: size)
        if !NSScreen.screens.contains(where: { $0.frame.intersects(rect) }),
           let main = NSScreen.main {
            NSLog("[Blurt] HUD off-screen at \(rect) on \(screen.frame); falling back to \(main.frame)")
            x = main.frame.midX - size.width / 2
            y = main.frame.minY + 140
        }
        w.setFrame(NSRect(origin: NSPoint(x: x, y: y), size: size), display: true)
    }

    /// The screen the user is currently working on. `NSScreen.main` tracks the
    /// key window, which for a background/click-through app resolves to the
    /// primary display — not where the user is. The screen under the mouse
    /// cursor is a far better proxy for "current screen".
    private static func activeScreen() -> NSScreen? {
        let mouse = NSEvent.mouseLocation
        return NSScreen.screens.first { NSMouseInRect(mouse, $0.frame, false) }
            ?? NSScreen.main
            ?? NSScreen.screens.first
    }
}
