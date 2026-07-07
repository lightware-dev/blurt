import AppKit

/// The Blurt visual identity, mirrored from the website (`www/src/app/global.css`):
/// near-black "HUD" ink, warm bone text, one loud highlighter-yellow accent, and a
/// coral rec dot. Shared by the onboarding window and the live-dictation HUD so the
/// app and the landing page feel like the same product.
enum Brand {
    private static func hex(_ v: UInt32) -> NSColor {
        NSColor(srgbRed: CGFloat((v >> 16) & 0xff) / 255,
                green: CGFloat((v >> 8) & 0xff) / 255,
                blue: CGFloat(v & 0xff) / 255,
                alpha: 1)
    }

    // ── palette ──────────────────────────────────────────────
    static let ink950 = hex(0x090a0c)
    static let ink900 = hex(0x0f1115)
    static let ink850 = hex(0x14161b)
    static let ink800 = hex(0x191c22)
    static let ink700 = hex(0x23262d)
    static let ink600 = hex(0x30343d)

    static let bone = hex(0xece6d8)
    static let boneDim = hex(0x8f8b7e)

    /// Highlighter yellow — the "we highlighted your mess" motif.
    static let marker = hex(0xe8ff32)
    /// The rec dot / small alarms.
    static let coral = hex(0xff5943)

    // ── type ─────────────────────────────────────────────────
    // The site uses Space Grotesk / Inter / JetBrains Mono. Those aren't
    // guaranteed on every Mac, so we lean on the system's own display and
    // monospaced faces, which carry the same tone.
    static func display(_ size: CGFloat, _ weight: NSFont.Weight = .bold) -> NSFont {
        NSFont.systemFont(ofSize: size, weight: weight)
    }

    static func mono(_ size: CGFloat, _ weight: NSFont.Weight = .regular) -> NSFont {
        NSFont.monospacedSystemFont(ofSize: size, weight: weight)
    }
}
