using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Media;
using System.Windows.Threading;

namespace Blurt;

/// A small borderless, click-through overlay near the bottom of the screen that
/// shows live partial transcription while dictating — the twin of the Mac
/// client's HUD.swift. Near-black ink pill, the live flowing waveform in brand
/// yellow on the left, live text in mono to its right. Topmost, never activates,
/// and passes clicks through so it never steals focus from the field being typed
/// into.
internal sealed class Hud
{
    private const double W = 560;
    private const double H = 76;
    // Head-truncation budget: keep the most recent words visible on the right,
    // dropping older text off the left behind an ellipsis (Mac uses byTruncatingHead).
    private const int MaxChars = 84;

    private Window? _window;
    private TextBlock? _label;
    private WaveformView? _wave;
    // Bumped on every show/hide so a scheduled flash auto-hide only fires if no
    // newer session has taken over the HUD in the meantime.
    private int _generation;

    public void Show(string text) => Show(text, "");

    /// Render a structured partial: the live segment at full strength, settled
    /// text receding behind it. With no text yet, `placeholder` is shown faded
    /// ("Listening…", or "Hearing you…" once the server's VAD confirms speech).
    public void Show(string committed, string live, string placeholder = "Listening…")
    {
        _generation++;
        Ensure();
        var isPlaceholder = string.IsNullOrEmpty(committed) && string.IsNullOrEmpty(live);
        var label = _label!;
        label.Inlines.Clear();
        if (isPlaceholder)
        {
            label.FontSize = 17;
            label.FontWeight = FontWeights.Normal;
            label.Foreground = Brand.BoneDimBrush;
            label.Text = placeholder;
        }
        else
        {
            label.FontSize = 18;
            label.FontWeight = FontWeights.Medium;
            // Head-truncate the combined text first, then split the visible part
            // into a settled run and the live run. The live segment — the words
            // being spoken right now — keeps the full-strength treatment the HUD
            // has always used; settled text behind it recedes to regular weight.
            // Emphasising it the other way round would dim the whole pill for a
            // typical dictation, since text only commits after a pause.
            var sep = committed.Length > 0 && live.Length > 0 ? " " : "";
            var full = committed + sep + live;
            var cut = Math.Max(0, full.Length - MaxChars);
            var visible = full[cut..];
            var committedVisible = Math.Max(0, committed.Length - cut);
            if (cut > 0)
                label.Inlines.Add(new System.Windows.Documents.Run("…") { Foreground = Brand.BoneDimBrush });
            if (committedVisible > 0)
                label.Inlines.Add(new System.Windows.Documents.Run(visible[..committedVisible])
                { Foreground = Brand.BoneBrush, FontWeight = FontWeights.Normal });
            if (visible.Length > committedVisible)
                label.Inlines.Add(new System.Windows.Documents.Run(visible[committedVisible..])
                { Foreground = Brand.BoneBrush, FontWeight = FontWeights.Medium });
        }
        // The waveform doubles as the rec indicator: it ripples while listening
        // and swells with the voice, so there's no separate rec dot.
        _wave!.Visibility = Visibility.Visible;

        Position();
        _window!.Show();
    }

    /// Feed the latest FFT frequency-band magnitudes into the spectrum meter.
    /// Must be called on the UI thread.
    public void Spectrum(float[] bands) => _wave?.SetBands(bands);

    /// Reflect the server's voice-activity state: the meter runs at full
    /// strength while the server reports speech and mutes when it doesn't, so
    /// the pill shows the whole audio path is alive — not just the local mic.
    /// Must be called on the UI thread.
    public void Hearing(bool on) => _wave?.SetHearing(on);

    public void Hide()
    {
        _generation++;
        _wave?.Reset();
        _window?.Hide();
    }

    /// Briefly show a dimmed status message (e.g. "Cancelled"), then auto-hide —
    /// unless a newer session has since taken over the HUD. The default suits an
    /// acknowledgement the user already expects; something they have to *read*
    /// (a connection failure) needs longer.
    public void Flash(string text, TimeSpan? duration = null)
    {
        _generation++;
        var token = _generation;
        Ensure();
        var label = _label!;
        label.Text = text;
        label.FontSize = 17;
        label.FontWeight = FontWeights.Normal;
        label.Foreground = Brand.BoneDimBrush;
        // Recording is over during a flash message — no meter; collapsing the wave
        // lets the label fill from the pill's leading edge so the text isn't oddly
        // indented (the Mac slides its label to the leading anchor for the same reason).
        _wave!.Reset();
        _wave.Visibility = Visibility.Collapsed;

        Position();
        _window!.Show();

        var timer = new DispatcherTimer { Interval = duration ?? TimeSpan.FromMilliseconds(300) };
        timer.Tick += (_, _) =>
        {
            timer.Stop();
            if (_generation == token) _window?.Hide();
        };
        timer.Start();
    }

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

        var wave = new WaveformView
        {
            Width = 96,
            Height = 34,
            VerticalAlignment = VerticalAlignment.Center,
            Margin = new Thickness(0, 0, 12, 0),
        };

        // Wave docked left; the label fills the rest. When a flash message
        // collapses the wave, the label reclaims its space and starts at the edge.
        var row = new DockPanel { LastChildFill = true, Margin = new Thickness(18, 0, 22, 0) };
        DockPanel.SetDock(wave, Dock.Left);
        row.Children.Add(wave);
        row.Children.Add(label);

        var card = new Border
        {
            Background = Brand.Solid(Brand.Ink900, 0.85),
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
        _wave = wave;
    }

    /// Place the HUD near the bottom of the screen under the mouse cursor — the
    /// Windows twin of HUD.activeScreen() on the Mac (WPF's own screen helpers
    /// track the primary display, not where the user is working). WinForms reports
    /// screen geometry in physical pixels; convert to WPF's device-independent
    /// units through the primary monitor's scale factor. That's exact when the
    /// monitors share a DPI — the common side-by-side case — and on mixed-DPI
    /// setups it can be slightly off but never lands on the wrong screen.
    private void Position()
    {
        try
        {
            var mouse = System.Windows.Forms.Control.MousePosition;
            var wa = System.Windows.Forms.Screen.FromPoint(mouse).WorkingArea; // excludes the taskbar
            double scale = System.Windows.Forms.Screen.PrimaryScreen is { } ps
                           && SystemParameters.PrimaryScreenWidth > 0
                ? ps.Bounds.Width / SystemParameters.PrimaryScreenWidth
                : 1.0;
            double left = wa.Left / scale, top = wa.Top / scale;
            double width = wa.Width / scale, height = wa.Height / scale;
            _window!.Left = left + (width - W) / 2;
            _window.Top = top + height - H - 120; // ~120 px above the taskbar
        }
        catch
        {
            // Fall back to the primary work area if the screen query fails.
            var area = SystemParameters.WorkArea;
            _window!.Left = area.Left + (area.Width - W) / 2;
            _window.Top = area.Bottom - H - 120;
        }
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
