import AppKit
import Carbon

/// Renders shortcuts as human-readable strings and converts AppKit modifier
/// flags to the Carbon masks RegisterEventHotKey expects.
enum ShortcutLabel {
    static func carbonMods(_ flags: NSEvent.ModifierFlags) -> UInt32 {
        var m: UInt32 = 0
        if flags.contains(.control) { m |= UInt32(controlKey) }
        if flags.contains(.option) { m |= UInt32(optionKey) }
        if flags.contains(.shift) { m |= UInt32(shiftKey) }
        if flags.contains(.command) { m |= UInt32(cmdKey) }
        return m
    }

    static func modifiers(_ carbon: UInt32) -> String {
        var s = ""
        if carbon & UInt32(controlKey) != 0 { s += "⌃" }
        if carbon & UInt32(optionKey) != 0 { s += "⌥" }
        if carbon & UInt32(shiftKey) != 0 { s += "⇧" }
        if carbon & UInt32(cmdKey) != 0 { s += "⌘" }
        return s
    }

    private static let special: [Int: String] = [
        kVK_Space: "Space", kVK_Return: "↩", kVK_Tab: "⇥", kVK_Escape: "⎋",
        kVK_Delete: "⌫", kVK_ForwardDelete: "⌦",
        kVK_LeftArrow: "←", kVK_RightArrow: "→", kVK_UpArrow: "↑", kVK_DownArrow: "↓",
        kVK_Home: "↖", kVK_End: "↘", kVK_PageUp: "⇞", kVK_PageDown: "⇟",
        kVK_F1: "F1", kVK_F2: "F2", kVK_F3: "F3", kVK_F4: "F4", kVK_F5: "F5",
        kVK_F6: "F6", kVK_F7: "F7", kVK_F8: "F8", kVK_F9: "F9", kVK_F10: "F10",
        kVK_F11: "F11", kVK_F12: "F12",
    ]

    static func isFunctionKey(_ code: UInt32) -> Bool {
        special[Int(code)]?.hasPrefix("F") == true
    }

    static func keyName(_ keyCode: UInt32) -> String {
        if let s = special[Int(keyCode)] { return s }
        // Everything else: ask the current keyboard layout what the key produces.
        guard let src = TISCopyCurrentKeyboardLayoutInputSource()?.takeRetainedValue(),
              let raw = TISGetInputSourceProperty(src, kTISPropertyUnicodeKeyLayoutData) else {
            return "key\(keyCode)"
        }
        let data = unsafeBitCast(raw, to: CFData.self) as Data
        return data.withUnsafeBytes { buf -> String in
            guard let layout = buf.bindMemory(to: UCKeyboardLayout.self).baseAddress else {
                return "key\(keyCode)"
            }
            var dead: UInt32 = 0
            var chars = [UniChar](repeating: 0, count: 4)
            var len = 0
            let err = UCKeyTranslate(layout, UInt16(keyCode), UInt16(kUCKeyActionDisplay), 0,
                                     UInt32(LMGetKbdType()), OptionBits(kUCKeyTranslateNoDeadKeysMask),
                                     &dead, chars.count, &len, &chars)
            guard err == noErr, len > 0 else { return "key\(keyCode)" }
            return String(utf16CodeUnits: chars, count: len).uppercased()
        }
    }

    static func describe(code: UInt32, mods: UInt32) -> String {
        modifiers(mods) + keyName(code)
    }

    /// The active shortcut for menu labels; empty when shortcuts are off.
    static func current() -> String {
        switch Settings.shortcutMode {
        case .doubleTap: return "⌥⌥"
        case .optionSpace: return "⌥Space"
        case .custom: return describe(code: Settings.hotKeyCode, mods: Settings.hotKeyMods)
        case .off: return ""
        }
    }
}

/// A click-to-record well: click it, press a combo, and it reports the keycode +
/// Carbon modifiers. Esc cancels. Bare keys (or shift-only combos) are rejected —
/// they'd swallow normal typing as a global hotkey — except function keys.
private final class ShortcutCaptureField: NSView {
    var onBegin: (() -> Void)?
    var onEnd: (() -> Void)?
    var onCapture: ((UInt32, UInt32) -> Void)?

    var isActive = true {
        didSet { alphaValue = isActive ? 1 : 0.35 }
    }

    private let label = NSTextField(labelWithString: "")
    private var capturing = false

    init() {
        super.init(frame: .zero)
        wantsLayer = true
        layer?.cornerRadius = 8
        layer?.borderWidth = 1
        layer?.backgroundColor = Brand.ink850.cgColor
        label.font = Brand.mono(13, .medium)
        label.alignment = .center
        label.translatesAutoresizingMaskIntoConstraints = false
        addSubview(label)
        NSLayoutConstraint.activate([
            label.centerXAnchor.constraint(equalTo: centerXAnchor),
            label.centerYAnchor.constraint(equalTo: centerYAnchor),
            widthAnchor.constraint(greaterThanOrEqualToConstant: 200),
            heightAnchor.constraint(equalToConstant: 34),
        ])
        refresh()
    }
    required init?(coder: NSCoder) { fatalError() }

    func refresh() {
        layer?.borderColor = (capturing ? Brand.marker : Brand.ink600).cgColor
        label.textColor = capturing ? Brand.marker : Brand.bone
        label.stringValue = capturing
            ? "type shortcut…"
            : ShortcutLabel.describe(code: Settings.hotKeyCode, mods: Settings.hotKeyMods)
    }

    override var acceptsFirstResponder: Bool { isActive }

    override func mouseDown(with event: NSEvent) {
        guard isActive else { return }
        window?.makeFirstResponder(self)
    }

    override func becomeFirstResponder() -> Bool {
        guard isActive else { return false }
        capturing = true
        refresh()
        onBegin?()
        return true
    }

    override func resignFirstResponder() -> Bool {
        if capturing {
            capturing = false
            refresh()
            onEnd?()
        }
        return true
    }

    override func keyDown(with event: NSEvent) { handle(event) }

    // Cmd-combos arrive here instead of keyDown; claim them while recording.
    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard capturing, event.type == .keyDown else { return false }
        handle(event)
        return true
    }

    override func flagsChanged(with event: NSEvent) {
        guard capturing else { return }
        let mods = ShortcutLabel.carbonMods(event.modifierFlags)
        label.stringValue = mods == 0 ? "type shortcut…" : ShortcutLabel.modifiers(mods)
    }

    private func handle(_ event: NSEvent) {
        guard capturing else { return }
        let mods = ShortcutLabel.carbonMods(event.modifierFlags)
        let code = UInt32(event.keyCode)
        if Int(code) == kVK_Escape, mods == 0 {
            window?.makeFirstResponder(nil)
            return
        }
        guard mods & ~UInt32(shiftKey) != 0 || ShortcutLabel.isFunctionKey(code) else {
            NSSound.beep()
            return
        }
        onCapture?(code, mods)
        window?.makeFirstResponder(nil)
    }
}

/// The Settings window. Currently holds one section: how to summon Blurt.
/// Changes apply (and persist) immediately.
final class SettingsWindowController: NSWindowController, NSWindowDelegate, NSTextFieldDelegate {

    /// Fired whenever the shortcut choice changes; the app re-arms its triggers.
    var onChange: (() -> Void)?
    /// Fired around custom-shortcut recording so the app can suspend its own
    /// triggers — otherwise the active hotkey would swallow the combo being typed.
    var onCaptureActive: ((Bool) -> Void)?

    private let colW: CGFloat = 300       // width of one card column
    private let colGap: CGFloat = 16      // gap between the two columns
    private var outerW: CGFloat { colW * 2 + colGap } // full content width
    private var cardInnerW: CGFloat { colW - 40 }     // width inside a card (colW − 2×20)

    private var radios: [Settings.ShortcutMode: NSButton] = [:]
    private var injectRadios: [String: NSButton] = [:]
    private let field = ShortcutCaptureField()
    private let serverField = NSTextField()
    private let tokenField = NSTextField()
    private let cursor = NSTextField(labelWithString: "▍")
    private var timer: Timer?
    private var blinkOn = true

    init() {
        let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 620),
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

        // ── brand row + headline, echoing the onboarding screen ──
        cursor.font = Brand.display(20, .bold)
        cursor.textColor = Brand.marker
        let word = label("Blurt", Brand.display(20, .bold), Brand.bone)
        let brand = NSStackView(views: [cursor, word])
        brand.orientation = .horizontal
        brand.spacing = 6
        brand.alignment = .firstBaseline

        let title = label("Settings", Brand.display(27, .bold), Brand.bone)
        let subtitle = wrapping("How you summon Blurt, and where your voice goes.",
                                Brand.display(14, .regular),
                                Brand.bone.withAlphaComponent(0.8), width: outerW)

        // ── card 01: shortcut ─────────────────────────────────
        let options: [(String, Settings.ShortcutMode)] = [
            ("Double-tap ⌥", .doubleTap),
            ("⌥ Space", .optionSpace),
            ("Custom shortcut", .custom),
            ("No shortcut  (menu bar only)", .off),
        ]
        var shortcutRows: [NSView] = []
        for (name, mode) in options {
            let radio = NSButton(radioButtonWithTitle: "", target: self, action: #selector(pick(_:)))
            radio.attributedTitle = NSAttributedString(string: name, attributes: [
                .font: Brand.display(14, .regular),
                .foregroundColor: Brand.bone,
            ])
            radios[mode] = radio
            shortcutRows.append(radio)
            if mode == .custom {
                // The capture well, indented under its radio.
                let indent = NSStackView(views: [spacer(24), field])
                indent.orientation = .horizontal
                shortcutRows.append(indent)
            }
        }
        shortcutRows.append(label("Click the well, then press a combo. Esc cancels.",
                                  Brand.mono(11), Brand.boneDim))
        let shortcutCard = makeCard(
            index: "01",
            title: "Shortcut",
            body: "How you summon Blurt from anywhere.",
            rows: shortcutRows)

        // ── card 02: server ───────────────────────────────────
        let urlLabel = label("WebSocket URL", Brand.display(13, .regular), Brand.bone)
        let tokenLabel = label("Auth token  (blank = none)", Brand.display(13, .regular), Brand.bone)
        let urlBox = boxed(serverField, placeholder: "wss://192.168.1.50:25878/ws",
                           value: Settings.serverURL)
        let tokenBox = boxed(tokenField, placeholder: "", value: Settings.authToken)
        let serverCard = makeCard(
            index: "02",
            title: "Server",
            body: "Your own transcription box — audio streams here and nowhere else.",
            rows: [urlLabel, urlBox, tokenLabel, tokenBox],
            tightAfter: [urlLabel, tokenLabel])

        // ── card 03: insertion ────────────────────────────────
        let insertRows: [(String, String)] = [
            ("Paste  (fast, uses the clipboard)", "paste"),
            ("Type  (slower, works in terminals)", "type"),
        ]
        var insertViews: [NSView] = []
        for (name, mode) in insertRows {
            let radio = NSButton(radioButtonWithTitle: "", target: self, action: #selector(pickInsert(_:)))
            radio.attributedTitle = NSAttributedString(string: name, attributes: [
                .font: Brand.display(14, .regular),
                .foregroundColor: Brand.bone,
            ])
            injectRadios[mode] = radio
            insertViews.append(radio)
        }
        let insertCard = makeCard(
            index: "03",
            title: "Insertion",
            body: "How the finished text lands in the focused field.",
            rows: insertViews)

        // ── assemble: card 01 on the left, 02 + 03 stacked on the right ──
        let rightCol = NSStackView(views: [serverCard, insertCard])
        rightCol.orientation = .vertical
        rightCol.alignment = .leading
        rightCol.spacing = 16

        let columns = NSStackView(views: [shortcutCard, rightCol])
        columns.orientation = .horizontal
        columns.alignment = .top
        columns.spacing = colGap

        let stack = NSStackView(views: [brand, title, subtitle, columns])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.setCustomSpacing(10, after: brand)
        stack.setCustomSpacing(22, after: subtitle)
        stack.translatesAutoresizingMaskIntoConstraints = false
        content.addSubview(stack)

        let pad: CGFloat = 28
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: pad),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -pad),
            stack.topAnchor.constraint(equalTo: content.topAnchor, constant: pad),
            stack.widthAnchor.constraint(equalToConstant: outerW),
            shortcutCard.widthAnchor.constraint(equalToConstant: colW),
            serverCard.widthAnchor.constraint(equalToConstant: colW),
            insertCard.widthAnchor.constraint(equalToConstant: colW),
        ])

        field.onBegin = { [weak self] in self?.onCaptureActive?(true) }
        field.onEnd = { [weak self] in self?.onCaptureActive?(false) }
        field.onCapture = { [weak self] code, mods in
            Settings.hotKeyCode = code
            Settings.hotKeyMods = mods
            Settings.shortcutMode = .custom
            self?.sync()
            self?.onChange?()
        }

        sync()
        content.layoutSubtreeIfNeeded()
        window?.setContentSize(NSSize(width: outerW + pad * 2,
                                      height: stack.fittingSize.height + pad * 2))
    }

    /// A numbered section card in the onboarding style: ink-900 rounded panel with
    /// a "01  Title" header, a dim body line, then the section's controls.
    private func makeCard(index: String, title: String, body: String,
                          rows: [NSView], tightAfter: [NSView] = []) -> NSView {
        let card = NSView()
        card.wantsLayer = true
        card.layer?.backgroundColor = Brand.ink900.cgColor
        card.layer?.cornerRadius = 16
        card.layer?.borderWidth = 1
        card.layer?.borderColor = Brand.ink700.cgColor

        let idx = label(index, Brand.mono(13, .medium), Brand.marker)
        let head = label(title, Brand.display(19, .bold), Brand.bone)
        let header = NSStackView(views: [idx, head])
        header.orientation = .horizontal
        header.alignment = .centerY
        header.spacing = 8

        let bodyLbl = wrapping(body, Brand.display(13, .regular),
                               Brand.bone.withAlphaComponent(0.72), width: cardInnerW)

        let inner = NSStackView(views: [header, bodyLbl] + rows)
        inner.orientation = .vertical
        inner.alignment = .leading
        inner.spacing = 12
        inner.setCustomSpacing(16, after: bodyLbl)
        for v in tightAfter { inner.setCustomSpacing(4, after: v) }
        inner.translatesAutoresizingMaskIntoConstraints = false
        card.addSubview(inner)

        let p: CGFloat = 20
        NSLayoutConstraint.activate([
            inner.leadingAnchor.constraint(equalTo: card.leadingAnchor, constant: p),
            inner.trailingAnchor.constraint(equalTo: card.trailingAnchor, constant: -p),
            inner.topAnchor.constraint(equalTo: card.topAnchor, constant: p),
            inner.bottomAnchor.constraint(equalTo: card.bottomAnchor, constant: -p),
        ])
        return card
    }

    private func wrapping(_ s: String, _ font: NSFont, _ color: NSColor, width: CGFloat) -> NSTextField {
        let t = label(s, font, color)
        t.lineBreakMode = .byWordWrapping
        t.maximumNumberOfLines = 0
        t.preferredMaxLayoutWidth = width
        t.widthAnchor.constraint(equalToConstant: width).isActive = true
        return t
    }

    private func label(_ s: String, _ font: NSFont, _ color: NSColor) -> NSTextField {
        let t = NSTextField(labelWithString: s)
        t.font = font
        t.textColor = color
        return t
    }

    private func spacer(_ w: CGFloat) -> NSView {
        let v = NSView()
        v.widthAnchor.constraint(equalToConstant: w).isActive = true
        return v
    }

    /// A text field vertically centered inside a styled well. (A bare NSTextField
    /// given extra height pins its text to the top — center it in a container
    /// instead and put the background/border on the container.)
    private func boxed(_ f: NSTextField, placeholder: String, value: String) -> NSView {
        f.stringValue = value
        f.font = Brand.mono(13, .medium)
        f.textColor = Brand.bone
        f.isBezeled = false
        f.isBordered = false
        f.drawsBackground = false
        f.focusRingType = .none
        f.delegate = self
        f.placeholderAttributedString = NSAttributedString(string: placeholder, attributes: [
            .font: Brand.mono(13, .medium),
            .foregroundColor: Brand.boneDim,
        ])
        f.translatesAutoresizingMaskIntoConstraints = false

        let box = NSView()
        box.wantsLayer = true
        box.layer?.backgroundColor = Brand.ink850.cgColor
        box.layer?.cornerRadius = 8
        box.layer?.borderWidth = 1
        box.layer?.borderColor = Brand.ink600.cgColor
        box.addSubview(f)
        NSLayoutConstraint.activate([
            f.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 10),
            f.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -10),
            f.centerYAnchor.constraint(equalTo: box.centerYAnchor),
            box.widthAnchor.constraint(equalToConstant: cardInnerW),
            box.heightAnchor.constraint(equalToConstant: 32),
        ])
        return box
    }

    /// Reflect Settings into the radios and the capture well.
    private func sync() {
        let mode = Settings.shortcutMode
        for (m, radio) in radios { radio.state = m == mode ? .on : .off }
        field.isActive = mode == .custom
        field.refresh()
        for (m, radio) in injectRadios { radio.state = m == Settings.injectMode ? .on : .off }
    }

    // MARK: actions

    @objc private func pick(_ sender: NSButton) {
        guard let mode = radios.first(where: { $0.value == sender })?.key else { return }
        Settings.shortcutMode = mode
        sync()
        onChange?()
        // Picking "Custom" starts recording right away.
        if mode == .custom { window?.makeFirstResponder(field) }
    }

    @objc private func pickInsert(_ sender: NSButton) {
        guard let mode = injectRadios.first(where: { $0.value == sender })?.key else { return }
        Settings.injectMode = mode
        sync()
    }

    /// Persist the server fields when editing ends (tab away, click away, close).
    func controlTextDidEndEditing(_ obj: Notification) {
        Settings.serverURL = serverField.stringValue.trimmingCharacters(in: .whitespaces)
        Settings.authToken = tokenField.stringValue.trimmingCharacters(in: .whitespaces)
    }

    // MARK: lifecycle

    func present() {
        showWindow(nil)
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
        // Blink the wordmark cursor, echoing the onboarding screen and the website.
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 0.55, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.blinkOn.toggle()
            self.cursor.alphaValue = self.blinkOn ? 1 : 0
        }
    }

    func windowWillClose(_ notification: Notification) {
        timer?.invalidate()
        timer = nil
        // Abandon any in-progress recording so triggers get re-armed.
        window?.makeFirstResponder(nil)
    }
}
