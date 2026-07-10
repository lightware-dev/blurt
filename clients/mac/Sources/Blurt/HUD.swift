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

    func show(_ text: String) {
        DispatchQueue.main.async {
            self.generation += 1
            if self.window == nil { self.build() }
            let isPlaceholder = text.isEmpty
            self.label.stringValue = isPlaceholder ? "Listening…" : text
            self.label.font = isPlaceholder ? Brand.mono(17) : Brand.mono(18, .medium)
            self.label.textColor = isPlaceholder ? Brand.boneDim : Brand.bone
            self.wave.isHidden = false
            self.labelAtLeading?.isActive = false
            self.labelAfterWave?.isActive = true
            self.position()
            self.window?.orderFrontRegardless()
        }
    }

    /// Feed the latest FFT frequency-band magnitudes into the spectrum meter.
    func spectrum(_ bands: [Float]) {
        DispatchQueue.main.async { self.wave.setBands(bands) }
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
            self.generation += 1
            let token = self.generation
            if self.window == nil { self.build() }
            self.label.stringValue = text
            self.label.font = Brand.mono(17)
            self.label.textColor = Brand.boneDim
            // Recording is over during a flash message — no meter; the label
            // takes over the wave's spot so the text isn't oddly indented.
            self.wave.reset()
            self.wave.isHidden = true
            self.labelAfterWave?.isActive = false
            self.labelAtLeading?.isActive = true
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
