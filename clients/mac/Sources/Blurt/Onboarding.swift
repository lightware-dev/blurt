import AppKit
import AVFoundation
import ApplicationServices

/// A button whose intrinsic size includes comfortable padding (NSButton with a
/// custom layer background otherwise hugs its title).
private final class PaddedButton: NSButton {
    var hPad: CGFloat = 18
    var vPad: CGFloat = 11
    override var intrinsicContentSize: NSSize {
        var s = super.intrinsicContentSize
        s.width += hPad * 2
        s.height += vPad * 2
        return s
    }
}

/// A small rounded status chip ("granted" / "needed" / "waiting…").
private final class StatusPill: NSView {
    private let lbl = NSTextField(labelWithString: "")

    init() {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 9
        lbl.font = Brand.mono(11, .medium)
        lbl.translatesAutoresizingMaskIntoConstraints = false
        addSubview(lbl)
        NSLayoutConstraint.activate([
            lbl.leadingAnchor.constraint(equalTo: leadingAnchor, constant: 10),
            lbl.trailingAnchor.constraint(equalTo: trailingAnchor, constant: -10),
            lbl.topAnchor.constraint(equalTo: topAnchor, constant: 4),
            lbl.bottomAnchor.constraint(equalTo: bottomAnchor, constant: -4),
        ])
    }
    required init?(coder: NSCoder) { fatalError() }

    func set(_ text: String, _ color: NSColor) {
        lbl.stringValue = text
        lbl.textColor = color
        layer?.backgroundColor = color.withAlphaComponent(0.14).cgColor
    }
}

/// First-run screen that explains the two macOS permissions Blurt needs — and
/// why — with live status and one-tap buttons to grant them. Styled to match the
/// website (dark ink, bone text, highlighter-yellow accent).
///
/// - Microphone: so Blurt can hear you.
/// - Accessibility: so Blurt can insert the finished text into the focused field.
final class OnboardingWindowController: NSWindowController, NSWindowDelegate {

    /// Called when both permissions are granted and the user hits "Start", or when
    /// they dismiss the window.
    var onClose: (() -> Void)?

    private let outerW: CGFloat = 504     // content column width
    private let cardInnerW: CGFloat = 464 // text width inside a card (outerW − 2×20)

    private var micPill = StatusPill()
    private var axPill = StatusPill()
    private var micButton = PaddedButton()
    private var axButton = PaddedButton()
    private var startButton = PaddedButton()
    private let cursor = NSTextField(labelWithString: "▍")

    private var timer: Timer?
    private var blinkOn = true

    init() {
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 700),
                           styleMask: [.titled, .closable, .fullSizeContentView],
                           backing: .buffered, defer: false)
        win.titleVisibility = .hidden
        win.titlebarAppearsTransparent = true
        win.isMovableByWindowBackground = true
        win.backgroundColor = Brand.ink950
        win.appearance = NSAppearance(named: .darkAqua)
        win.isReleasedWhenClosed = false
        win.standardWindowButton(.miniaturizeButton)?.isHidden = true
        win.standardWindowButton(.zoomButton)?.isHidden = true
        super.init(window: win)
        win.delegate = self
        buildUI()
        win.center()
    }
    required init?(coder: NSCoder) { fatalError() }

    // MARK: build

    private func buildUI() {
        guard let content = window?.contentView else { return }

        // ── brand row: blinking marker cursor + wordmark ──────
        cursor.font = Brand.display(20, .bold)
        cursor.textColor = Brand.marker
        let word = label("Blurt", Brand.display(20, .bold), Brand.bone)
        let brand = NSStackView(views: [cursor, word])
        brand.orientation = .horizontal
        brand.spacing = 6
        brand.alignment = .firstBaseline

        // ── headline + subtitle ───────────────────────────────
        let title = label("Two permissions, then talk.", Brand.display(27, .bold), Brand.bone)
        let subtitle = wrapping(
            "Blurt turns speech into text entirely on your Mac — no account, no cloud. macOS just needs you to allow two things first.",
            Brand.display(14, .regular), Brand.bone.withAlphaComponent(0.8), width: outerW)

        // ── permission cards ──────────────────────────────────
        let mic = makeCard(
            index: "01",
            title: "Microphone",
            body: "So Blurt can hear you. Audio streams to your own server for transcription and is never stored.",
            steps: [],
            pill: micPill,
            button: micButton,
            buttonTitle: "Grant Microphone",
            action: #selector(handleMic))

        let ax = makeCard(
            index: "02",
            title: "Accessibility",
            body: "So Blurt can type the finished text into whatever field you're using. macOS counts this as controlling your Mac, so you switch it on by hand:",
            steps: [
                "1   Open  System Settings ▸ Privacy & Security ▸ Accessibility",
                "2   Turn on the switch next to Blurt (add it with + if it isn't listed)",
            ],
            pill: axPill,
            button: axButton,
            buttonTitle: "Open Accessibility Settings",
            action: #selector(handleAccessibility))

        // ── footer: privacy note + actions ────────────────────
        let note = label("no account · no cloud · your voice never leaves this box",
                         Brand.mono(11), Brand.boneDim)

        configure(startButton, title: "Start Blurting  →", filled: true, action: #selector(handleStart))
        let skip = PaddedButton()
        configure(skip, title: "I'll grant these later", filled: false, bordered: false, action: #selector(handleSkip))
        let actions = NSStackView(views: [startButton, skip])
        actions.orientation = .horizontal
        actions.spacing = 12

        // ── assemble ──────────────────────────────────────────
        let stack = NSStackView(views: [brand, title, subtitle, mic, ax, note, actions])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.setCustomSpacing(10, after: brand)
        stack.setCustomSpacing(22, after: subtitle)
        stack.setCustomSpacing(24, after: ax)
        stack.setCustomSpacing(18, after: note)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        let pad: CGFloat = 28
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: pad),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -pad),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: pad),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -pad),
            stack.widthAnchor.constraint(equalToConstant: outerW),
            mic.widthAnchor.constraint(equalToConstant: outerW),
            ax.widthAnchor.constraint(equalToConstant: outerW),
        ])

        // Size the window to fit the assembled content.
        content.layoutSubtreeIfNeeded()
        window?.setContentSize(NSSize(width: outerW + pad * 2,
                                      height: stack.fittingSize.height + pad * 2))
        refresh()
    }

    private func makeCard(index: String, title: String, body: String, steps: [String],
                          pill: StatusPill, button: PaddedButton,
                          buttonTitle: String, action: Selector) -> NSView {
        let card = NSView()
        card.wantsLayer = true
        card.layer?.backgroundColor = Brand.ink900.cgColor
        card.layer?.cornerRadius = 16
        card.layer?.borderWidth = 1
        card.layer?.borderColor = Brand.ink700.cgColor

        // header: "01  Microphone" on the left, status pill on the right
        let idx = label(index, Brand.mono(13, .medium), Brand.marker)
        let head = label(title, Brand.display(19, .bold), Brand.bone)
        let left = NSStackView(views: [idx, head])
        left.orientation = .horizontal
        left.alignment = .centerY
        left.spacing = 8

        let header = NSView()
        for v in [left, pill] { v.translatesAutoresizingMaskIntoConstraints = false; header.addSubview(v) }
        NSLayoutConstraint.activate([
            left.leadingAnchor.constraint(equalTo: header.leadingAnchor),
            left.topAnchor.constraint(equalTo: header.topAnchor),
            left.bottomAnchor.constraint(equalTo: header.bottomAnchor),
            pill.trailingAnchor.constraint(equalTo: header.trailingAnchor),
            pill.centerYAnchor.constraint(equalTo: left.centerYAnchor),
        ])

        let bodyLbl = wrapping(body, Brand.display(13, .regular),
                               Brand.bone.withAlphaComponent(0.72), width: cardInnerW)

        var rows: [NSView] = [header, bodyLbl]
        if !steps.isEmpty {
            let stepLabels = steps.map { wrapping($0, Brand.mono(12), Brand.boneDim, width: cardInnerW) }
            let stepStack = NSStackView(views: stepLabels)
            stepStack.orientation = .vertical
            stepStack.alignment = .leading
            stepStack.spacing = 6
            rows.append(stepStack)
        }
        configure(button, title: buttonTitle, filled: true, action: action)
        rows.append(button)

        let inner = NSStackView(views: rows)
        inner.orientation = .vertical
        inner.alignment = .leading
        inner.spacing = 12
        inner.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(inner)

        let p: CGFloat = 20
        NSLayoutConstraint.activate([
            inner.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: p),
            inner.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -p),
            inner.topAnchor.constraint(equalTo: card.topAnchor, constant: p),
            inner.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -p),
            header.widthAnchor.constraint(equalTo: inner.widthAnchor),
        ])
        return card
    }

    // MARK: small view helpers

    private func label(_ s: String, _ font: NSFont, _ color: NSColor) -> NSTextField {
        let t = NSTextField(labelWithString: s)
        t.font = font
        t.textColor = color
        return t
    }

    private func wrapping(_ s: String, _ font: NSFont, _ color: NSColor, width: CGFloat) -> NSTextField {
        let t = label(s, font, color)
        t.lineBreakMode = .byWordWrapping
        t.maximumNumberOfLines = 0
        t.preferredMaxLayoutWidth = width
        t.widthAnchor.constraint(equalToConstant: width).isActive = true
        return t
    }

    private func configure(_ b: PaddedButton, title: String, filled: Bool,
                           bordered: Bool = true, action: Selector) {
        b.target = self
        b.action = action
        b.isBordered = false
        b.bezelStyle = .regularSquare
        b.wantsLayer = true
        b.layer?.cornerRadius = 10
        b.layer?.backgroundColor = filled ? Brand.marker.cgColor : NSColor.clear.cgColor
        b.layer?.borderWidth = (!filled && bordered) ? 1 : 0
        b.layer?.borderColor = Brand.ink600.cgColor
        b.attributedTitle = NSAttributedString(string: title, attributes: [
            .font: Brand.display(14, .bold),
            .foregroundColor: filled ? Brand.ink950 : Brand.bone,
        ])
    }

    // MARK: permission state

    private func micStatus() -> AVAuthorizationStatus {
        AVCaptureDevice.authorizationStatus(for: .audio)
    }

    private func refresh() {
        let mic = micStatus()
        let ax = AXIsProcessTrusted()

        switch mic {
        case .authorized:
            micPill.set("granted", Brand.marker)
            setGranted(micButton, "Microphone granted")
        case .denied, .restricted:
            micPill.set("blocked", Brand.coral)
            configure(micButton, title: "Open Microphone Settings", filled: true, action: #selector(handleMic))
        default:
            micPill.set("needed", Brand.coral)
            configure(micButton, title: "Grant Microphone", filled: true, action: #selector(handleMic))
        }

        if ax {
            axPill.set("granted", Brand.marker)
            setGranted(axButton, "Accessibility granted")
        } else {
            axPill.set("waiting…", Brand.boneDim)
            configure(axButton, title: "Open Accessibility Settings", filled: true, action: #selector(handleAccessibility))
        }

        let ready = (mic == .authorized) && ax
        startButton.isEnabled = ready
        startButton.alphaValue = ready ? 1 : 0.4
    }

    /// Turn a granted permission's button into a dim, disabled "✓ …" chip.
    private func setGranted(_ b: PaddedButton, _ text: String) {
        b.isEnabled = false
        b.layer?.backgroundColor = NSColor.clear.cgColor
        b.layer?.borderWidth = 1
        b.layer?.borderColor = Brand.ink700.cgColor
        b.attributedTitle = NSAttributedString(string: "✓  " + text, attributes: [
            .font: Brand.display(14, .bold),
            .foregroundColor: Brand.marker,
        ])
    }

    // MARK: actions

    @objc private func handleMic() {
        switch micStatus() {
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .audio) { [weak self] _ in
                DispatchQueue.main.async { self?.refresh() }
            }
        default:
            // Already decided — the system won't reprompt, so send them to Settings.
            open("x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")
        }
    }

    @objc private func handleAccessibility() {
        // Nudge macOS to list Blurt, then open the pane so the user can flip it on.
        let opts = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
        _ = AXIsProcessTrustedWithOptions(opts)
        open("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
    }

    @objc private func handleStart() { window?.performClose(nil) }
    @objc private func handleSkip() { window?.performClose(nil) }

    private func open(_ urlString: String) {
        if let url = URL(string: urlString) { NSWorkspace.shared.open(url) }
    }

    // MARK: lifecycle

    func present() {
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        // Poll for permission changes (Accessibility flips outside our process) and
        // blink the wordmark cursor, echoing the website.
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.55, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.blinkOn.toggle()
            self.cursor.alphaValue = self.blinkOn ? 1 : 0
            self.refresh()
        }
    }

    func windowWillClose(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
        onClose?()
    }
}
