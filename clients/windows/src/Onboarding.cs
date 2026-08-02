using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;

namespace Blurt;

/// First-run setup screen, the twin of the Mac client's Onboarding.swift, styled
/// to match the website (dark ink, bone text, highlighter-yellow accent).
///
/// Windows needs no Accessibility grant to inject text, so the scary half of the
/// Mac flow is gone. What's left: point Blurt at your server, confirm a mic is
/// heard, and optionally start at sign-in.
internal sealed class Onboarding : Window
{
    public Action? OnClose;

    private readonly TextBox _serverField;
    private readonly TextBox _tokenField;
    private readonly CheckBox _startAtLogin;
    private readonly StatusPill _micPill;
    private readonly TextBlock _cursor;
    private readonly Button _micButton;
    private readonly DispatcherTimer _blink;
    private bool _blinkOn = true;
    private AudioCapture? _micTest;

    public Onboarding()
    {
        Title = "Blurt Setup";
        Width = 560;
        SizeToContent = SizeToContent.Height;
        WindowStyle = WindowStyle.SingleBorderWindow;
        ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Background = Brand.Ink950Brush;

        _serverField = InputField(Settings.ServerUrl, "wss://192.168.1.50:25878/ws");
        _tokenField = InputField(Settings.AuthToken, "blank = none");
        _micPill = new StatusPill();
        _cursor = new TextBlock
        {
            Text = "▍",
            FontFamily = Brand.Display,
            FontSize = 20,
            FontWeight = FontWeights.Bold,
            Foreground = Brand.MarkerBrush,
        };
        _startAtLogin = new CheckBox
        {
            Content = "Start Blurt when I sign in",
            IsChecked = Settings.StartAtLogin,
            Foreground = Brand.BoneBrush,
            FontFamily = Brand.Display,
            FontSize = 13,
            Margin = new Thickness(0, 4, 0, 0),
        };
        _micButton = SecondaryButton("Test microphone");
        _micButton.Click += (_, _) => RunMicTest();

        BuildUi();

        _blink = new DispatcherTimer { Interval = TimeSpan.FromSeconds(0.55) };
        _blink.Tick += (_, _) => { _blinkOn = !_blinkOn; _cursor.Opacity = _blinkOn ? 1 : 0; };
        _blink.Start();

        RefreshMic();
        Closed += (_, _) =>
        {
            _blink.Stop();
            _micTest?.Dispose();
            OnClose?.Invoke();
        };
    }

    private void BuildUi()
    {
        var brandRow = new StackPanel { Orientation = Orientation.Horizontal };
        brandRow.Children.Add(_cursor);
        brandRow.Children.Add(new TextBlock
        {
            Text = "Blurt",
            FontFamily = Brand.Display,
            FontSize = 20,
            FontWeight = FontWeights.Bold,
            Foreground = Brand.BoneBrush,
            Margin = new Thickness(6, 0, 0, 0),
        });

        var title = Heading("Point it at your server, then talk.", 26);
        var subtitle = Body(
            "Blurt turns speech into text using your own Parakeet server — no account, " +
            "no public cloud. Tell it where the server lives and you're set.",
            Brand.Solid(Brand.Bone, 0.8), 14);

        var serverCard = MakeCard("01", "Server", new UIElement[]
        {
            Body("The WebSocket URL of your Blurt server. Blurt asks you to confirm " +
                 "its self-signed certificate once, then trusts only that one.",
                 Brand.Solid(Brand.Bone, 0.72), 13),
            FieldLabel("Server URL"), _serverField,
            FieldLabel("Auth token (optional)"), _tokenField,
        });

        var micRow = new StackPanel { Orientation = Orientation.Horizontal };
        micRow.Children.Add(_micButton);

        var micCard = MakeCard("02", "Microphone", new UIElement[]
        {
            Body("So Blurt can hear you. Audio streams to your own server for " +
                 "transcription and is never stored. If Windows blocks the mic, " +
                 "allow desktop apps under Settings ▸ Privacy ▸ Microphone.",
                 Brand.Solid(Brand.Bone, 0.72), 13),
            micRow,
        }, headerRight: _micPill);

        var note = new TextBlock
        {
            Text = "no account · no public cloud · your voice never leaves your LAN",
            FontFamily = Brand.Mono,
            FontSize = 11,
            Foreground = Brand.BoneDimBrush,
            Margin = new Thickness(0, 4, 0, 0),
        };

        var start = PrimaryButton("Start Blurting  →");
        start.Click += (_, _) => Finish();
        var skip = SecondaryButton("I'll set this up later", bordered: false);
        skip.Click += (_, _) => Finish();

        var actions = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(0, 6, 0, 0) };
        actions.Children.Add(start);
        skip.Margin = new Thickness(12, 0, 0, 0);
        actions.Children.Add(skip);

        var stack = new StackPanel { Margin = new Thickness(28) };
        void Add(UIElement e, double gap = 16) { e.SetTopMargin(gap); stack.Children.Add(e); }
        brandRow.SetTopMargin(0); stack.Children.Add(brandRow);
        Add(title, 12);
        Add(subtitle, 8);
        Add(serverCard, 22);
        Add(micCard, 16);
        Add(_startAtLogin, 18);
        Add(note, 16);
        Add(actions, 18);

        Content = stack;
    }

    // ── mic test ─────────────────────────────────────────────
    private void RefreshMic()
    {
        if (!AudioCapture.HasInputDevice)
            _micPill.Set("no mic found", Brand.Coral);
        else
            _micPill.Set("ready", Brand.BoneDim);
    }

    private void RunMicTest()
    {
        if (!AudioCapture.HasInputDevice) { _micPill.Set("no mic found", Brand.Coral); return; }

        _micTest?.Dispose();
        _micPill.Set("listening…", Brand.Marker);
        _micButton.IsEnabled = false;

        var heardSound = false;
        var capture = new AudioCapture();
        capture.OnFrame = (buffer, count) =>
        {
            // Peak-detect over the Int16 samples; anything above the noise floor
            // proves the OS is actually handing us audio.
            for (var i = 0; i + 1 < count; i += 2)
            {
                var sample = (short)(buffer[i] | (buffer[i + 1] << 8));
                if (Math.Abs((int)sample) > 500) { heardSound = true; break; }
            }
        };
        _micTest = capture;
        capture.Start();

        var stop = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1.6) };
        stop.Tick += (_, _) =>
        {
            stop.Stop();
            capture.Stop();
            _micButton.IsEnabled = true;
            if (heardSound) _micPill.Set("heard you ✓", Brand.Marker);
            else _micPill.Set("silent — check mic", Brand.Coral);
        };
        stop.Start();
    }

    private void Finish()
    {
        Settings.ServerUrl = _serverField.Text.Trim();
        Settings.AuthToken = _tokenField.Text.Trim();
        var login = _startAtLogin.IsChecked == true;
        Settings.StartAtLogin = login;
        StartupRegistration.Apply(login);
        Settings.DidOnboard = true;
        Close();
    }

    // ── view helpers ─────────────────────────────────────────
    private static TextBlock Heading(string s, double size) => new()
    {
        Text = s,
        FontFamily = Brand.Display,
        FontSize = size,
        FontWeight = FontWeights.Bold,
        Foreground = Brand.BoneBrush,
        TextWrapping = TextWrapping.Wrap,
    };

    private static TextBlock Body(string s, Brush color, double size) => new()
    {
        Text = s,
        FontFamily = Brand.Display,
        FontSize = size,
        Foreground = color,
        TextWrapping = TextWrapping.Wrap,
        LineHeight = size * 1.45,
    };

    private static TextBlock FieldLabel(string s) => new()
    {
        Text = s,
        FontFamily = Brand.Mono,
        FontSize = 11,
        Foreground = Brand.BoneDimBrush,
        Margin = new Thickness(0, 10, 0, 4),
    };

    private static TextBox InputField(string value, string placeholder) => new()
    {
        Text = value,
        FontFamily = Brand.Mono,
        FontSize = 13,
        Padding = new Thickness(8, 6, 8, 6),
        Background = Brand.Ink950Brush,
        Foreground = Brand.BoneBrush,
        CaretBrush = Brand.MarkerBrush,
        BorderBrush = Brand.Ink700Brush,
        BorderThickness = new Thickness(1),
        ToolTip = placeholder,
    };

    private Border MakeCard(string index, string title, UIElement[] rows, UIElement? headerRight = null)
    {
        var idx = new TextBlock
        {
            Text = index,
            FontFamily = Brand.Mono,
            FontSize = 13,
            FontWeight = FontWeights.Medium,
            Foreground = Brand.MarkerBrush,
            VerticalAlignment = VerticalAlignment.Center,
        };
        var head = new TextBlock
        {
            Text = title,
            FontFamily = Brand.Display,
            FontSize = 18,
            FontWeight = FontWeights.Bold,
            Foreground = Brand.BoneBrush,
            Margin = new Thickness(8, 0, 0, 0),
            VerticalAlignment = VerticalAlignment.Center,
        };
        var left = new StackPanel { Orientation = Orientation.Horizontal };
        left.Children.Add(idx);
        left.Children.Add(head);

        var header = new DockPanel { LastChildFill = false };
        DockPanel.SetDock(left, Dock.Left);
        header.Children.Add(left);
        if (headerRight is not null)
        {
            DockPanel.SetDock(headerRight, Dock.Right);
            if (headerRight is FrameworkElement fe) fe.VerticalAlignment = VerticalAlignment.Center;
            header.Children.Add(headerRight);
        }

        var inner = new StackPanel();
        header.SetTopMargin(0);
        inner.Children.Add(header);
        foreach (var r in rows) { r.SetTopMargin(10); inner.Children.Add(r); }

        return new Border
        {
            Background = Brand.Ink900Brush,
            CornerRadius = new CornerRadius(16),
            BorderBrush = Brand.Ink700Brush,
            BorderThickness = new Thickness(1),
            Padding = new Thickness(20),
            Child = inner,
        };
    }

    private static Button PrimaryButton(string text) => StyledButton(text, filled: true, bordered: true);
    private static Button SecondaryButton(string text, bool bordered = true) =>
        StyledButton(text, filled: false, bordered: bordered);

    private static Button StyledButton(string text, bool filled, bool bordered) => new()
    {
        Content = text,
        Padding = new Thickness(18, 10, 18, 10),
        FontFamily = Brand.Display,
        FontSize = 14,
        FontWeight = FontWeights.Bold,
        Cursor = System.Windows.Input.Cursors.Hand,
        HorizontalAlignment = HorizontalAlignment.Left,
        Foreground = filled ? Brand.Ink950Brush : Brand.BoneBrush,
        Background = filled ? Brand.MarkerBrush : Brushes.Transparent,
        BorderBrush = Brand.Ink600Brush,
        BorderThickness = new Thickness(bordered && !filled ? 1 : 0),
    };

    /// A small rounded status chip ("ready" / "heard you ✓" / "no mic found").
    private sealed class StatusPill : Border
    {
        private readonly TextBlock _label;
        public StatusPill()
        {
            CornerRadius = new CornerRadius(9);
            Padding = new Thickness(10, 4, 10, 4);
            HorizontalAlignment = HorizontalAlignment.Left;
            _label = new TextBlock { FontFamily = Brand.Mono, FontSize = 11, FontWeight = FontWeights.Medium };
            Child = _label;
        }
        public void Set(string text, Color color)
        {
            _label.Text = text;
            _label.Foreground = Brand.Solid(color);
            Background = Brand.Solid(color, 0.14);
        }
    }
}

internal static class FrameworkElementExtensions
{
    /// Sets only the top margin, leaving the element's own horizontal margins —
    /// a terse stand-in for AppKit's setCustomSpacing on a vertical stack.
    public static void SetTopMargin(this UIElement e, double top)
    {
        if (e is FrameworkElement fe)
            fe.Margin = new Thickness(fe.Margin.Left, top, fe.Margin.Right, fe.Margin.Bottom);
    }
}
