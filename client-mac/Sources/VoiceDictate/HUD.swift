import AppKit

/// A small borderless, click-through overlay near the bottom of the screen that
/// shows live partial transcription while dictating.
final class HUD {
    private var window: NSWindow?
    private let label = NSTextField(labelWithString: "")

    func show(_ text: String) {
        DispatchQueue.main.async {
            if self.window == nil { self.build() }
            let isPlaceholder = text.isEmpty
            self.label.stringValue = isPlaceholder ? "Listening…" : text
            self.label.textColor = isPlaceholder ? NSColor(white: 1, alpha: 0.4) : .white
            self.position()
            self.window?.orderFrontRegardless()
        }
    }

    func hide() {
        DispatchQueue.main.async { self.window?.orderOut(nil) }
    }

    private func build() {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 560, height: 90),
                         styleMask: .borderless, backing: .buffered, defer: false)
        w.level = .statusBar
        w.isOpaque = false
        w.backgroundColor = .clear
        w.hasShadow = true
        w.ignoresMouseEvents = true
        w.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary]

        let container = NSView()
        container.wantsLayer = true
        container.layer?.backgroundColor = NSColor(white: 0.08, alpha: 0.92).cgColor
        container.layer?.cornerRadius = 16
        container.layer?.masksToBounds = true   // clip the label to the box

        label.font = .systemFont(ofSize: 19, weight: .medium)
        label.textColor = .white
        // Single line, fixed width: truncate the *head* so the most recent words
        // stay visible on the right and older text scrolls off the left with an "…".
        label.maximumNumberOfLines = 1
        label.lineBreakMode = .byTruncatingHead
        label.usesSingleLineMode = true
        label.cell?.truncatesLastVisibleLine = true
        // Let the label shrink/truncate rather than push the box wider.
        label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        label.setContentHuggingPriority(.defaultLow, for: .horizontal)
        label.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 20),
            label.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -20),
            label.centerYAnchor.constraint(equalTo: container.centerYAnchor),
        ])
        w.contentView = container
        window = w
    }

    private func position() {
        guard let w = window, let screen = NSScreen.main else { return }
        let size = NSSize(width: 560, height: 90)
        let x = screen.frame.midX - size.width / 2
        let y = screen.frame.minY + 140
        w.setFrame(NSRect(origin: NSPoint(x: x, y: y), size: size), display: true)
    }
}
