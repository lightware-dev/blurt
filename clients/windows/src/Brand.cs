using System.Windows.Media;
using WpfColor = System.Windows.Media.Color;
using GdiColor = System.Drawing.Color;

namespace Blurt;

/// The Blurt visual identity, mirrored from the website (www/src/app/global.css)
/// and the Mac client's Brand.swift: near-black "HUD" ink, warm bone text, one
/// loud highlighter-yellow accent, and a coral rec dot. Shared by the onboarding
/// window, the live HUD, and the tray icon so the app and the landing page feel
/// like the same product.
internal static class Brand
{
    private static WpfColor Hex(uint v) =>
        WpfColor.FromRgb((byte)((v >> 16) & 0xff), (byte)((v >> 8) & 0xff), (byte)(v & 0xff));

    // ── palette ──────────────────────────────────────────────
    public static readonly WpfColor Ink950 = Hex(0x090a0c);
    public static readonly WpfColor Ink900 = Hex(0x0f1115);
    public static readonly WpfColor Ink850 = Hex(0x14161b);
    public static readonly WpfColor Ink800 = Hex(0x191c22);
    public static readonly WpfColor Ink700 = Hex(0x23262d);
    public static readonly WpfColor Ink600 = Hex(0x30343d);

    public static readonly WpfColor Bone = Hex(0xece6d8);
    public static readonly WpfColor BoneDim = Hex(0x8f8b7e);

    /// Highlighter yellow — the "we highlighted your mess" motif.
    public static readonly WpfColor Marker = Hex(0xe8ff32);
    /// The rec dot / small alarms.
    public static readonly WpfColor Coral = Hex(0xff5943);

    // ── brushes (frozen, so they're cheap to reuse across the UI) ──
    public static readonly Brush Ink950Brush = Freeze(Ink950);
    public static readonly Brush Ink900Brush = Freeze(Ink900);
    public static readonly Brush Ink700Brush = Freeze(Ink700);
    public static readonly Brush Ink600Brush = Freeze(Ink600);
    public static readonly Brush BoneBrush = Freeze(Bone);
    public static readonly Brush BoneDimBrush = Freeze(BoneDim);
    public static readonly Brush MarkerBrush = Freeze(Marker);
    public static readonly Brush CoralBrush = Freeze(Coral);

    public static SolidColorBrush Solid(WpfColor c, double alpha = 1.0)
    {
        var b = new SolidColorBrush(c) { Opacity = alpha };
        b.Freeze();
        return b;
    }

    private static Brush Freeze(WpfColor c)
    {
        var b = new SolidColorBrush(c);
        b.Freeze();
        return b;
    }

    // ── type ─────────────────────────────────────────────────
    // The site uses Space Grotesk / Inter / JetBrains Mono. Those aren't
    // guaranteed on Windows, so we lean on Segoe UI for display and Cascadia /
    // Consolas for mono, which carry the same tone. The comma-lists are WPF
    // fallback chains — the first installed family wins.
    public static readonly FontFamily Display = new("Segoe UI Variable Display, Segoe UI");
    public static readonly FontFamily Mono = new("Cascadia Mono, Cascadia Code, Consolas");

    // GDI equivalents for the tray icon, which is drawn with System.Drawing.
    public static GdiColor GdiBone => GdiColor.FromArgb(0xec, 0xe6, 0xd8);
    public static GdiColor GdiCoral => GdiColor.FromArgb(0xff, 0x59, 0x43);
}
