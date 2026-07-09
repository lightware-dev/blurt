import AppKit

/// A small borderless, click-through overlay near the bottom of the screen that
/// shows live partial transcription while dictating. Styled to match the website's
/// HUD (see `www` — the animated demo box): near-black ink pill, a glowing coral
/// rec dot, and the live text in mono.
final class HUD {
    private var window: NSWindow?
    private let label = NSTextField(labelWithString: "")
    private let dot = NSView()

    private let size = NSSize(width: 560, height: 76)
    // Bumped on every show/hide so a scheduled flash auto-hide only fires if no
    // newer session has taken over the HUD in the meantime.
    private var generation = 0

    func show(_ text: String) {
        DispatchQueue.main.async {
            self.generation += 1
            if self.window == nil { self.build() }
            let isPlaceholder = text.isEmpty
            self.label.stringValue = isPlaceholder ? "Listening…" : text
            self.label.font = isPlaceholder ? Brand.mono(17) : Brand.mono(18, .medium)
            self.label.textColor = isPlaceholder ? Brand.boneDim : Brand.bone
            self.position()
            self.window?.orderFrontRegardless()
        }
    }

    func hide() {
        DispatchQueue.main.async {
            self.generation += 1
            self.window?.orderOut(nil)
        }
    }

    /// Briefly show a dimmed status message (e.g. "Cancelled"), then auto-hide —
    /// unless a newer session has since taken over the HUD.
    func flash(_ text: String) {
        DispatchQueue.main.async {
            self.generation += 1
            let token = self.generation
            if self.window == nil { self.build() }
            self.label.stringValue = text
            self.label.font = Brand.mono(17)
            self.label.textColor = Brand.boneDim
            self.position()
            self.window?.orderFrontRegardless()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
                if self.generation == token { self.window?.orderOut(nil) }
            }
        }
    }

    private func build() {
        let w = NSWindow(contentRect: NSRect(origin: .zero, size: size),
                         styleMask: .borderless, backing: .buffered, defer: false)
        w.level = .statusBar
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.ignoresMouseEvents = true
        w.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        let container = NSView()
        container.wantsLayer = true
        container.layer?.backgroundColor = Brand.ink900.withAlphaComponent(0.95).cgColor
        container.layer?.cornerRadius = 18
        container.layer?.borderWidth = 1
        container.layer?.borderColor = Brand.ink700.cgColor
        container.layer?.masksToBounds = true

        // Glowing coral rec dot, like the demo's "Blurting…" indicator.
        dot.wantsLayer = true
        dot.layer?.backgroundColor = Brand.coral.cgColor
        dot.layer?.cornerRadius = 5
        dot.layer?.shadowColor = Brand.coral.cgColor
        dot.layer?.shadowOpacity = 0.9
        dot.layer?.shadowRadius = 6
        dot.layer?.shadowOffset = .zero
        dot.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(dot)

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

        NSLayoutConstraint.activate([
            dot.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 22),
            dot.centerYAnchor.constraint(equalTo: container.centerYAnchor),
            dot.widthAnchor.constraint(equalToConstant: 10),
            dot.heightAnchor.constraint(equalToConstant: 10),

            label.leadingAnchor.constraint(equalTo: dot.trailingAnchor, constant: 14),
            label.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -22),
            label.centerYAnchor.constraint(equalTo: container.centerYAnchor),
        ])
        w.contentView = container
        window = w
    }

    private func position() {
        guard let w = window, let screen = Self.activeScreen() else { return }
        let x = screen.frame.midX - size.width / 2
        let y = screen.frame.minY + 140
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
