using System.IO;
using System.Text.Json;

namespace Blurt;

/// Persisted config, the Windows twin of the Mac client's Settings.swift. Backed
/// by a JSON file at %APPDATA%\Blurt\config.json instead of UserDefaults. The
/// default hotkey is Ctrl+Alt+Space (Windows reserves plain Alt+Space for the
/// window system menu, so we can't reuse the Mac's ⌥Space).
internal static class Settings
{
    private sealed class Model
    {
        public string ServerUrl { get; set; } = "wss://localhost:25878/ws";
        public string AuthToken { get; set; } = "";
        public string InjectMode { get; set; } = "paste"; // "paste" or "type"
        public bool DidOnboard { get; set; }
        public bool StartAtLogin { get; set; }
        public string Shortcut { get; set; } = "doubleTap"; // ShortcutMode name
        // Custom chord: a Win32 virtual-key + a MOD_* modifier mask. Defaults to
        // Ctrl+Alt+Space so a fresh "Custom" pick isn't an empty, unregisterable combo.
        public uint HotKeyVk { get; set; } = 0x20;                 // VK_SPACE
        public uint HotKeyMods { get; set; } = 0x0002 | 0x0001;    // MOD_CONTROL | MOD_ALT
    }

    private static readonly string Dir =
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Blurt");
    private static readonly string File = Path.Combine(Dir, "config.json");

    private static readonly Model Current = Load();
    private static readonly JsonSerializerOptions JsonOpts = new() { WriteIndented = true };

    private static Model Load()
    {
        try
        {
            if (System.IO.File.Exists(File))
                return JsonSerializer.Deserialize<Model>(System.IO.File.ReadAllText(File)) ?? new Model();
        }
        catch { /* corrupt/unreadable → fall back to defaults */ }
        return new Model();
    }

    private static void Save()
    {
        try
        {
            Directory.CreateDirectory(Dir);
            System.IO.File.WriteAllText(File, JsonSerializer.Serialize(Current, JsonOpts));
        }
        catch { /* best-effort; don't crash dictation over a failed write */ }
    }

    public static string ServerUrl
    {
        get => Current.ServerUrl;
        set { Current.ServerUrl = value; Save(); }
    }

    public static string AuthToken
    {
        get => Current.AuthToken;
        set { Current.AuthToken = value; Save(); }
    }

    /// "paste" (fast, clipboard + Ctrl+V) or "type" (SendInput unicode, works in
    /// terminals and apps that ignore synthetic paste).
    public static string InjectMode
    {
        get => Current.InjectMode;
        set { Current.InjectMode = value; Save(); }
    }

    /// Whether the first-run setup screen has been dismissed at least once.
    public static bool DidOnboard
    {
        get => Current.DidOnboard;
        set { Current.DidOnboard = value; Save(); }
    }

    public static bool StartAtLogin
    {
        get => Current.StartAtLogin;
        set { Current.StartAtLogin = value; Save(); }
    }

    /// How dictation is summoned. `Custom` uses HotKeyVk/HotKeyMods. Defaults to
    /// DoubleTap (double-tap Ctrl), matching the Mac client's double-tap ⌥ default.
    public enum ShortcutMode { DoubleTap, CtrlAltSpace, Custom, Off }

    public static ShortcutMode Shortcut
    {
        get => Enum.TryParse<ShortcutMode>(Current.Shortcut, out var m) ? m : ShortcutMode.DoubleTap;
        set { Current.Shortcut = value.ToString(); Save(); }
    }

    /// Win32 virtual-key code of the custom chord.
    public static uint HotKeyVk
    {
        get => Current.HotKeyVk;
        set { Current.HotKeyVk = value; Save(); }
    }

    /// MOD_* modifier mask of the custom chord (as RegisterHotKey expects).
    public static uint HotKeyMods
    {
        get => Current.HotKeyMods;
        set { Current.HotKeyMods = value; Save(); }
    }
}
