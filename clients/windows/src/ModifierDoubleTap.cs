using System.Runtime.InteropServices;

namespace Blurt;

/// Fires when Ctrl is tapped twice in quick succession, alone — the Windows twin
/// of the Mac client's ModifierDoubleTap.swift (which double-taps ⌥). A "tap" is a
/// clean press-and-release of Ctrl with no other key or modifier in between and a
/// short hold, so Ctrl-as-chord-modifier (Ctrl+C, Ctrl+V…) never triggers it.
///
/// macOS can watch modifier *flag* transitions directly; Windows has no such event,
/// so we install a global low-level keyboard hook (WH_KEYBOARD_LL) and reconstruct
/// the modifier state from individual key up/down events. The hook callback is
/// delivered on the thread that installed it (the WPF UI thread), so `onFire` is
/// posted asynchronously to avoid stalling the system-wide input pipeline.
internal sealed class ModifierDoubleTap : IDisposable
{
    /// Longest a Ctrl press can be held and still count as a tap (ms).
    private const long MaxHold = 350;
    /// Longest gap between the two taps, first release → second release (ms).
    private const long MaxGap = 450;

    private readonly Action _onFire;
    private readonly LowLevelKeyboardProc _proc; // kept alive so the GC can't collect it
    private readonly IntPtr _hook;

    // Which modifier keys are currently held (normalized: Ctrl/Alt/Shift/Win).
    private readonly HashSet<Mod> _mods = new();
    private long? _ctrlDownAt;  // Ctrl is cleanly held alone, pressed at this tick
    private long? _firstTapAt;  // a clean first tap completed, awaiting the second
    private bool _dirty;        // another key/modifier was seen while Ctrl was held

    public ModifierDoubleTap(Action onFire)
    {
        _onFire = onFire;
        _proc = HookProc;
        using var module = System.Diagnostics.Process.GetCurrentProcess().MainModule!;
        _hook = SetWindowsHookEx(WH_KEYBOARD_LL, _proc, GetModuleHandle(module.ModuleName), 0);
    }

    private enum Mod { Ctrl, Alt, Shift, Win }

    private static Mod? ModifierOf(uint vk) => vk switch
    {
        0x11 or 0xA2 or 0xA3 => Mod.Ctrl,  // VK_CONTROL / L / R
        0x12 or 0xA4 or 0xA5 => Mod.Alt,   // VK_MENU / L / R
        0x10 or 0xA0 or 0xA1 => Mod.Shift, // VK_SHIFT / L / R
        0x5B or 0x5C => Mod.Win,           // VK_LWIN / VK_RWIN
        _ => null,
    };

    private IntPtr HookProc(int nCode, IntPtr wParam, IntPtr lParam)
    {
        if (nCode >= 0)
        {
            var msg = (int)wParam;
            var vk = Marshal.ReadInt32(lParam); // KBDLLHOOKSTRUCT.vkCode is the first field
            var down = msg is WM_KEYDOWN or WM_SYSKEYDOWN;
            Handle((uint)vk, down);
        }
        return CallNextHookEx(_hook, nCode, wParam, lParam);
    }

    private void Handle(uint vk, bool down)
    {
        var mod = ModifierOf(vk);
        var now = Environment.TickCount64;

        if (mod is null)
        {
            // A regular key: any keydown dirties an in-flight Ctrl hold and cancels a
            // pending first tap (mirrors the Swift keyDown monitor's reset()).
            if (down) Reset();
            return;
        }

        var changed = down ? _mods.Add(mod.Value) : _mods.Remove(mod.Value);
        if (!changed) return; // ignore auto-repeat keydowns for an already-held modifier

        if (_mods.Count == 1 && _mods.Contains(Mod.Ctrl))
        {
            // Ctrl is now the only modifier down — start (or restart) a clean hold.
            _ctrlDownAt = now;
            _dirty = false;
        }
        else if (_mods.Count == 0)
        {
            // All modifiers released — evaluate whether that was a clean Ctrl tap.
            var downAt = _ctrlDownAt;
            _ctrlDownAt = null;
            if (downAt is null || _dirty || now - downAt.Value >= MaxHold) { _firstTapAt = null; return; }
            if (_firstTapAt is { } first && now - first < MaxGap)
            {
                _firstTapAt = null;
                _onFire();
            }
            else
            {
                _firstTapAt = now;
            }
        }
        else
        {
            // Some other modifier (or a combination) is down: not a clean tap.
            Reset();
        }
    }

    private void Reset()
    {
        _dirty = _ctrlDownAt is not null;
        _firstTapAt = null;
    }

    public void Dispose()
    {
        if (_hook != IntPtr.Zero) UnhookWindowsHookEx(_hook);
    }

    // ── Win32 low-level keyboard hook plumbing ───────────────
    private const int WH_KEYBOARD_LL = 13;
    private const int WM_KEYDOWN = 0x0100;
    private const int WM_SYSKEYDOWN = 0x0104; // Alt is held → key events arrive as "sys" messages

    private delegate IntPtr LowLevelKeyboardProc(int nCode, IntPtr wParam, IntPtr lParam);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(int idHook, LowLevelKeyboardProc lpfn, IntPtr hMod, uint dwThreadId);
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool UnhookWindowsHookEx(IntPtr hhk);
    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
    [DllImport("kernel32.dll", CharSet = CharSet.Auto, SetLastError = true)]
    private static extern IntPtr GetModuleHandle(string lpModuleName);
}
