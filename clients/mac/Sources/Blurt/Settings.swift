import Foundation
import Carbon

/// Persisted config, backed by UserDefaults. Hotkey defaults to double-tap ⌥.
enum Settings {
    private static let d = UserDefaults.standard

    static var serverURL: String {
        get { d.string(forKey: "serverURL") ?? "wss://localhost:25878/ws" }
        set { d.set(newValue, forKey: "serverURL") }
    }

    static var authToken: String {
        get { d.string(forKey: "authToken") ?? "" }
        set { d.set(newValue, forKey: "authToken") }
    }

    /// "paste" (fast, clipboard) or "type" (CGEvent unicode, works in terminals).
    static var injectMode: String {
        get { d.string(forKey: "injectMode") ?? "paste" }
        set { d.set(newValue, forKey: "injectMode") }
    }

    static var hotKeyCode: UInt32 {
        get { (d.object(forKey: "hotKeyCode") as? NSNumber)?.uint32Value ?? UInt32(kVK_Space) }
        set { d.set(NSNumber(value: newValue), forKey: "hotKeyCode") }
    }

    static var hotKeyMods: UInt32 {
        get { (d.object(forKey: "hotKeyMods") as? NSNumber)?.uint32Value ?? UInt32(optionKey) }
        set { d.set(NSNumber(value: newValue), forKey: "hotKeyMods") }
    }

    /// How dictation is summoned. `custom` uses `hotKeyCode`/`hotKeyMods`.
    enum ShortcutMode: String {
        case doubleTap    // tap ⌥ twice (default)
        case optionSpace  // ⌥Space
        case custom       // user-recorded key combo
        case off          // menu bar only
    }

    static var shortcutMode: ShortcutMode {
        get { ShortcutMode(rawValue: d.string(forKey: "shortcutMode") ?? "") ?? .doubleTap }
        set { d.set(newValue.rawValue, forKey: "shortcutMode") }
    }

    /// SHA-256 fingerprints of the self-signed server certificates the user has
    /// trusted, keyed by lowercased `host:port` (see CertTrust). Per-host so
    /// that pointing the client at a different server doesn't drop trust for the
    /// old one, and so localhost and a LAN address are tracked separately.
    private static let pinnedCertsKey = "pinnedCerts"

    static func pinnedFingerprint(host: String) -> String? {
        (d.dictionary(forKey: pinnedCertsKey) as? [String: String])?[host]
    }

    static func setPinnedFingerprint(_ fingerprint: String, host: String) {
        var pins = (d.dictionary(forKey: pinnedCertsKey) as? [String: String]) ?? [:]
        pins[host] = fingerprint
        d.set(pins, forKey: pinnedCertsKey)
    }

    /// Whether the first-run permissions screen has been dismissed at least once.
    static var didOnboard: Bool {
        get { d.bool(forKey: "didOnboard") }
        set { d.set(newValue, forKey: "didOnboard") }
    }
}
