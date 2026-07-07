using Microsoft.Win32;

namespace Blurt;

/// Registers/unregisters Blurt in the per-user Run key so it launches at sign-in.
/// Per-user (HKCU) needs no elevation — the Windows analog of a macOS login item.
internal static class StartupRegistration
{
    private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "Blurt";

    public static void Apply(bool enabled)
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKey, writable: true);
            if (key is null) return;
            if (enabled)
            {
                var exe = Environment.ProcessPath ?? "";
                if (exe.Length > 0) key.SetValue(ValueName, $"\"{exe}\"");
            }
            else
            {
                key.DeleteValue(ValueName, throwOnMissingValue: false);
            }
        }
        catch { /* best-effort; a failed toggle shouldn't crash the app */ }
    }
}
