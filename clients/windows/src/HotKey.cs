using System.Runtime.InteropServices;
using System.Windows.Interop;

namespace Blurt;

/// System-wide hotkey via Win32 RegisterHotKey, the twin of the Mac client's
/// HotKey.swift (Carbon RegisterEventHotKey). It needs a window to receive
/// WM_HOTKEY, so we spin up a tiny invisible HwndSource purely as a message sink
/// and hook its wndproc. Default chord is Ctrl+Alt+Space.
internal sealed class HotKey : IDisposable
{
    private const int WM_HOTKEY = 0x0312;
    private const uint MOD_ALT = 0x0001;
    private const uint MOD_CONTROL = 0x0002;
    private const uint MOD_SHIFT = 0x0004;
    private const uint MOD_NOREPEAT = 0x4000; // fire once per press, not on auto-repeat
    public const uint VK_SPACE = 0x20;

    [DllImport("user32.dll")]
    private static extern bool RegisterHotKey(IntPtr hWnd, int id, uint fsModifiers, uint vk);
    [DllImport("user32.dll")]
    private static extern bool UnregisterHotKey(IntPtr hWnd, int id);

    private readonly HwndSource _source;
    private readonly Action _onFire;
    private const int Id = 0x42_31; // 'B1'

    /// True when the OS accepted the registration. Null-returning ctor would be
    /// un-C#; callers check this instead (mirrors the Swift init? contract).
    public bool Registered { get; }

    public HotKey(uint modifiers, uint virtualKey, Action onFire)
    {
        _onFire = onFire;

        // A zero-size, never-shown top-level window is enough to receive WM_HOTKEY.
        var parameters = new HwndSourceParameters("BlurtHotKeySink")
        {
            Width = 0,
            Height = 0,
            WindowStyle = 0,
        };
        _source = new HwndSource(parameters);
        _source.AddHook(WndProc);

        Registered = RegisterHotKey(_source.Handle, Id, modifiers | MOD_NOREPEAT, virtualKey);
    }

    /// Convenience for the app's default chord: Ctrl+Alt+Space.
    public static HotKey CtrlAltSpace(Action onFire) =>
        new(MOD_CONTROL | MOD_ALT, VK_SPACE, onFire);

    private IntPtr WndProc(IntPtr hwnd, int msg, IntPtr wParam, IntPtr lParam, ref bool handled)
    {
        if (msg == WM_HOTKEY && wParam.ToInt32() == Id)
        {
            _onFire();
            handled = true;
        }
        return IntPtr.Zero;
    }

    public void Dispose()
    {
        if (Registered) UnregisterHotKey(_source.Handle, Id);
        _source.RemoveHook(WndProc);
        _source.Dispose();
    }
}
