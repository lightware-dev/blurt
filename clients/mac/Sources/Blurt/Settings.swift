import Foundation
import Carbon

/// Persisted config, backed by UserDefaults. Hotkey defaults to ⌥Space.
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

    /// Whether the first-run permissions screen has been dismissed at least once.
    static var didOnboard: Bool {
        get { d.bool(forKey: "didOnboard") }
        set { d.set(newValue, forKey: "didOnboard") }
    }
}
