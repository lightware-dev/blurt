using System.Windows;

namespace Blurt;

/// Entry point, the twin of the Mac client's main.swift. A WPF Application with no
/// startup window — Blurt lives in the tray. ShutdownMode.OnExplicitShutdown keeps
/// it alive when the onboarding/HUD windows close; only "Quit" ends the process.
internal static class Program
{
    [STAThread]
    private static void Main()
    {
        var app = new Application { ShutdownMode = ShutdownMode.OnExplicitShutdown };
        var blurt = new BlurtApp(app);
        app.Startup += (_, _) => blurt.Start();
        app.Exit += (_, _) => blurt.Dispose();
        app.Run();
    }
}
