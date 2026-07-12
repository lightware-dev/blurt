import AppKit

/// Fires when Option is tapped twice in quick succession, alone. A "tap" is a
/// clean press-and-release of Option with no other key or modifier in between
/// and a short hold — so Option-as-typing-modifier (⌥e, accents, symbols) and
/// chorded shortcuts never trigger it.
///
/// Uses NSEvent global monitors, which need Accessibility trust — already
/// required by TextInjector, so this adds no new permission.
final class ModifierDoubleTap {
    private let onFire: () -> Void
    private var monitors: [Any] = []

    /// Longest an Option press can be held and still count as a tap.
    private let maxHold: TimeInterval = 0.35
    /// Longest gap between the two taps (first release → second release).
    private let maxGap: TimeInterval = 0.45

    private var downAt: Date?       // Option is currently held, pressed at this time
    private var firstTapAt: Date?   // a clean first tap completed, awaiting the second
    private var dirty = false       // another key/modifier was seen while Option was held

    init(onFire: @escaping () -> Void) {
        self.onFire = onFire
        let flags: (NSEvent) -> Void = { [weak self] in self?.flagsChanged($0) }
        let keys: (NSEvent) -> Void = { [weak self] _ in self?.reset() }
        monitors = [
            NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged, handler: flags) as Any,
            NSEvent.addGlobalMonitorForEvents(matching: .keyDown, handler: keys) as Any,
            NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { flags($0); return $0 } as Any,
            NSEvent.addLocalMonitorForEvents(matching: .keyDown) { keys($0); return $0 } as Any,
        ]
    }

    deinit {
        monitors.forEach { NSEvent.removeMonitor($0) }
    }

    private func flagsChanged(_ event: NSEvent) {
        // Deliberately excludes .capsLock: it stays latched in the flags and would
        // otherwise block detection whenever Caps Lock is on.
        let mask: NSEvent.ModifierFlags = [.shift, .control, .option, .command, .function]
        let mods = event.modifierFlags.intersection(mask)
        let now = Date()

        if mods == .option {
            downAt = now
            dirty = false
        } else if mods.isEmpty {
            defer { downAt = nil }
            guard let downAt, !dirty, now.timeIntervalSince(downAt) < maxHold else {
                firstTapAt = nil
                return
            }
            if let firstTapAt, now.timeIntervalSince(firstTapAt) < maxGap {
                self.firstTapAt = nil
                onFire()
            } else {
                firstTapAt = now
            }
        } else {
            // Some other modifier (or a combination) is down: not a clean tap.
            reset()
        }
    }

    private func reset() {
        dirty = downAt != nil
        firstTapAt = nil
    }
}
