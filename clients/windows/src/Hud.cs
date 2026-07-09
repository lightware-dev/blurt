using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Shapes;
using System.Windows.Threading;

namespace Blurt;

/// A small borderless, click-through overlay near the bottom of the screen that
/// shows live partial transcription while dictating — the twin of the Mac
/// client's HUD.swift. Near-black ink pill, a glowing coral rec dot, live text in
/// mono. Topmost, never activates, and passes clicks through so it never steals
/// focus from the field being typed into.
internal sealed class Hud
{
    private const double W = 560;
    private const double H = 76;
    // Head-truncation budget: keep the most recent words visible on the right,
    // dropping older text off the left behind an ellipsis (Mac uses byTruncatingHead).
    private const int MaxChars = 84;

    private Window? _window;
    private TextBlock? _label;
    // Bumped on every show/hide so a scheduled flash auto-hide only fires if no
    // newer session has taken over the HUD in the meantime.
    private int _generation;

    public void Show(string text)
    {
        _generation++;
        Ensure();
        var placeholder = string.IsNullOrEmpty(text);
        var label = _label!;
        label.Text = placeholder ? "Listening…" : Tail(text);
        label.FontSize = placeholder ? 17 : 18;
        label.FontWeight = placeholder ? FontWeights.Normal : FontWeights.Medium;
        label.Foreground = placeholder ? Brand.BoneDimBrush : Brand.BoneBrush;

        Position();
        _window!.Show();
    }

    public void Hide()
    {
        _generation++;
        _window?.Hide();
    }

    /// Briefly show a dimmed status message (e.g. "Cancelled"), then auto-hide —
    /// unless a newer session has since taken over the HUD.
    public void Flash(string text)
    {
        _generation++;
        var token = _generation;
        Ensure();
        var label = _label!;
        label.Text = text;
        label.FontSize = 17;
        label.FontWeight = FontWeights.Normal;
        label.Foreground = Brand.BoneDimBrush;

        Position();
        _window!.Show();

        var timer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(300) };
        timer.Tick += (_, _) =>
        {
            timer.Stop();
            if (_generation == token) _window?.Hide();
        };
        timer.Start();
    }

    private static string Tail(string s) =>
        s.Length <= MaxChars ? s : "…" + s[^MaxChars..];

    private void Ensure()
    {
        if (_window is not null) return;

        var label = new TextBlock
        {
            FontFamily = Brand.Mono,
            FontSize = 18,
            FontWeight = FontWeights.Medium,
            Foreground = Brand.BoneBrush,
            TextTrimming = TextTrimming.CharacterEllipsis,
            TextWrapping = TextWrapping.NoWrap,
            VerticalAlignment = VerticalAlignment.Center,
        };

        var dot = new Ellipse
        {
            Width = 10,
            Height = 10,
            Fill = Brand.CoralBrush,
            VerticalAlignment = VerticalAlignment.Center,
            Effect = new DropShadowEffect
            {
                Color = Brand.Coral,
                BlurRadius = 12,
                ShadowDepth = 0,
                Opacity = 0.9,
            },
        };

        var row = new DockPanel { LastChildFill = true, Margin = new Thickness(22, 0, 22, 0) };
        DockPanel.SetDock(dot, Dock.Left);
        dot.Margin = new Thickness(0, 0, 14, 0);
        row.Children.Add(dot);
        row.Children.Add(label);

        var card = new Border
        {
            Background = Brand.Solid(Brand.Ink900, 0.95),
            CornerRadius = new CornerRadius(18),
            BorderBrush = Brand.Ink700Brush,
            BorderThickness = new Thickness(1),
            Child = row,
        };

        var window = new Window
        {
            Width = W,
            Height = H,
            WindowStyle = WindowStyle.None,
            AllowsTransparency = true,
            Background = Brushes.Transparent,
            ShowInTaskbar = false,
            ShowActivated = false,
            Topmost = true,
            ResizeMode = ResizeMode.NoResize,
            Content = card,
        };
        window.SourceInitialized += (_, _) => MakeClickThrough(window);

        _window = window;
        _label = label;
    }

    private void Position()
    {
        var area = SystemParameters.WorkArea; // excludes the taskbar
        _window!.Left = area.Left + (area.Width - W) / 2;
        _window.Top = area.Bottom - H - 120; // ~120 px above the taskbar
    }

    // ── click-through / no-activate via extended window styles ──
    private const int GWL_EXSTYLE = -20;
    private const int WS_EX_TRANSPARENT = 0x0020;
    private const int WS_EX_LAYERED = 0x0080000;
    private const int WS_EX_TOOLWINDOW = 0x0080;   // keep it out of Alt-Tab
    private const int WS_EX_NOACTIVATE = 0x08000000;

    [DllImport("user32.dll")] private static extern int GetWindowLong(IntPtr h, int i);
    [DllImport("user32.dll")] private static extern int SetWindowLong(IntPtr h, int i, int v);

    private static void MakeClickThrough(Window w)
    {
        var hwnd = new WindowInteropHelper(w).Handle;
        var ex = GetWindowLong(hwnd, GWL_EXSTYLE);
        SetWindowLong(hwnd, GWL_EXSTYLE,
            ex | WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE);
    }
}
