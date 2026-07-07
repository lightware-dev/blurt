using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Threading;

namespace Blurt;

/// Inserts text into whatever field currently has focus — the twin of the Mac
/// client's TextInjector.swift. Unlike macOS, Windows needs no Accessibility
/// grant to synthesize input, so this "just works" for any focused app.
///
/// Two modes (Settings.InjectMode):
///   "paste" — put text on the clipboard, synthesize Ctrl+V, restore clipboard.
///   "type"  — emit each UTF-16 unit as a KEYEVENTF_UNICODE keystroke (works in
///             terminals and apps that ignore synthetic paste).
///
/// Must be called on the UI (STA) thread — the clipboard demands it.
internal static class TextInjector
{
    public static void Inject(string text)
    {
        if (string.IsNullOrEmpty(text)) return;
        if (Settings.InjectMode == "type") TypeUnicode(text);
        else Paste(text);
    }

    // Fast path: stash the clipboard, replace it with our text, Ctrl+V, then
    // restore the previous contents a moment later (mirrors the Mac 0.25 s delay).
    private static void Paste(string text)
    {
        var dispatcher = Application.Current?.Dispatcher ?? Dispatcher.CurrentDispatcher;

        string? previous = null;
        try { if (Clipboard.ContainsText()) previous = Clipboard.GetText(); } catch { }
        try { Clipboard.SetText(text); } catch { TypeUnicode(text); return; }

        PressCtrlV();

        dispatcher.BeginInvoke(() =>
        {
            try
            {
                if (previous is not null) Clipboard.SetText(previous);
                else Clipboard.Clear();
            }
            catch { }
        }, DispatcherPriority.Background);
    }

    private static void PressCtrlV()
    {
        const ushort VK_CONTROL = 0x11;
        const ushort VK_V = 0x56;
        var inputs = new[]
        {
            KeyDown(VK_CONTROL),
            KeyDown(VK_V),
            KeyUp(VK_V),
            KeyUp(VK_CONTROL),
        };
        SendInput((uint)inputs.Length, inputs, Marshal.SizeOf<INPUT>());
    }

    // Robust path: emit each UTF-16 code unit as a Unicode key event pair. Slower
    // but independent of the focused app's paste handling.
    private static void TypeUnicode(string text)
    {
        var inputs = new List<INPUT>(text.Length * 2);
        foreach (var unit in text) // char iteration == UTF-16 code units
        {
            inputs.Add(UnicodeDown(unit));
            inputs.Add(UnicodeUp(unit));
        }
        if (inputs.Count == 0) return;
        var arr = inputs.ToArray();
        SendInput((uint)arr.Length, arr, Marshal.SizeOf<INPUT>());
    }

    // ── Win32 SendInput plumbing ─────────────────────────────
    private const uint INPUT_KEYBOARD = 1;
    private const uint KEYEVENTF_KEYUP = 0x0002;
    private const uint KEYEVENTF_UNICODE = 0x0004;

    private static INPUT KeyDown(ushort vk) => Key(vk, 0, 0);
    private static INPUT KeyUp(ushort vk) => Key(vk, 0, KEYEVENTF_KEYUP);
    private static INPUT UnicodeDown(char c) => Key(0, c, KEYEVENTF_UNICODE);
    private static INPUT UnicodeUp(char c) => Key(0, c, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP);

    private static INPUT Key(ushort vk, ushort scan, uint flags) => new()
    {
        type = INPUT_KEYBOARD,
        u = new InputUnion { ki = new KEYBDINPUT { wVk = vk, wScan = scan, dwFlags = flags } },
    };

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT { public uint type; public InputUnion u; }

    // The union must be sized to its largest member (MOUSEINPUT), otherwise
    // Marshal.SizeOf<INPUT>() is too small, cbSize mismatches what Windows
    // expects, and SendInput silently no-ops on x64. MOUSEINPUT is only here to
    // reserve that size — we never populate it.
    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)] public KEYBDINPUT ki;
        [FieldOffset(0)] public MOUSEINPUT mi;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT
    {
        public int dx;
        public int dy;
        public uint mouseData;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }
}
