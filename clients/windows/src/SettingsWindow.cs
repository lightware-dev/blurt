using System.Media;
using System.Runtime.InteropServices;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Input;
using System.Windows.Media;

namespace Blurt;

/// Renders shortcuts as human-readable strings and converts WPF modifier flags to
/// the MOD_* masks RegisterHotKey expects. The twin of the Mac client's
/// ShortcutLabel (which speaks Carbon masks + ⌘⌥⌃⇧ glyphs).
internal static class ShortcutLabel
{
    public const uint MOD_ALT = 0x0001;
    public const uint MOD_CONTROL = 0x0002;
    public const uint MOD_SHIFT = 0x0004;
    public const uint MOD_WIN = 0x0008;

    public static uint ModsFromWpf(ModifierKeys m)
    {
        uint r = 0;
        if (m.HasFlag(ModifierKeys.Control)) r |= MOD_CONTROL;
        if (m.HasFlag(ModifierKeys.Alt)) r |= MOD_ALT;
        if (m.HasFlag(ModifierKeys.Shift)) r |= MOD_SHIFT;
        if (m.HasFlag(ModifierKeys.Windows)) r |= MOD_WIN;
        return r;
    }

    public static string Modifiers(uint mods)
    {
        var s = "";
        if ((mods & MOD_CONTROL) != 0) s += "Ctrl+";
        if ((mods & MOD_ALT) != 0) s += "Alt+";
        if ((mods & MOD_SHIFT) != 0) s += "Shift+";
        if ((mods & MOD_WIN) != 0) s += "Win+";
        return s;
    }

    private static readonly Dictionary<uint, string> Special = new()
    {
        [0x20] = "Space", [0x0D] = "Enter", [0x09] = "Tab", [0x1B] = "Esc",
        [0x08] = "Backspace", [0x2E] = "Del", [0x2D] = "Ins",
        [0x25] = "←", [0x26] = "↑", [0x27] = "→", [0x28] = "↓",
        [0x24] = "Home", [0x23] = "End", [0x21] = "PgUp", [0x22] = "PgDn",
    };

    /// VK_F1 (0x70) … VK_F24 (0x87). Function keys are the one bare-key family we
    /// allow as a global shortcut — they don't collide with normal typing.
    public static bool IsFunctionKey(uint vk) => vk is >= 0x70 and <= 0x87;

    public static string KeyName(uint vk)
    {
        if (Special.TryGetValue(vk, out var s)) return s;
        if (IsFunctionKey(vk)) return "F" + (vk - 0x70 + 1);
        if (vk is >= 0x30 and <= 0x5A) return ((char)vk).ToString(); // 0-9, A-Z
        var ch = MapVirtualKey(vk, 2 /* MAPVK_VK_TO_CHAR */) & 0x7fff;
        return ch != 0 ? char.ToUpper((char)ch).ToString() : $"Key{vk}";
    }

    public static string Describe(uint vk, uint mods) => Modifiers(mods) + KeyName(vk);

    /// The active shortcut, for the tray toggle label; empty when shortcuts are off.
    public static string Current() => Settings.Shortcut switch
    {
        Settings.ShortcutMode.DoubleTap => "Ctrl Ctrl",
        Settings.ShortcutMode.CtrlAltSpace => "Ctrl+Alt+Space",
        Settings.ShortcutMode.Custom => Describe(Settings.HotKeyVk, Settings.HotKeyMods),
        _ => "",
    };

    [DllImport("user32.dll")]
    private static extern uint MapVirtualKey(uint uCode, uint uMapType);
}

/// The Settings window — the twin of the Mac client's SettingsWindow.swift. Three
/// cards: how you summon Blurt (the shortcut, including a click-to-record custom
/// chord), where your voice goes (server), and how the text lands (insertion).
/// Changes apply and persist immediately.
internal sealed class SettingsWindow : Window
{
    /// Fired whenever the shortcut choice changes; the app re-arms its triggers.
    public Action? OnChange;
    /// Fired around custom-shortcut recording so the app can suspend its own
    /// triggers — otherwise the active hotkey would swallow the combo being typed.
    public Action<bool>? OnCaptureActive;
    /// Fired when the server URL actually changes, so the app can settle trust in
    /// the new server's certificate before the next dictation needs it.
    public Action? OnServerChanged;
    public Action? OnClose;

    private readonly Dictionary<Settings.ShortcutMode, RadioButton> _shortcutRadios = new();
    private readonly Dictionary<string, RadioButton> _insertRadios = new();
    private readonly ShortcutCaptureField _field = new();
    private readonly TextBox _serverField;
    private readonly TextBox _tokenField;
    private bool _syncing;

    public SettingsWindow()
    {
        Title = "Blurt Settings";
        Width = 560;
        SizeToContent = SizeToContent.Height;
        WindowStyle = WindowStyle.SingleBorderWindow;
        ResizeMode = ResizeMode.NoResize;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        Background = Brand.Ink950Brush;

        _serverField = InputField(Settings.ServerUrl, "wss://192.168.1.50:25878/ws");
        _tokenField = InputField(Settings.AuthToken, "blank = none");

        _field.OnBegin = () => OnCaptureActive?.Invoke(true);
        _field.OnEnd = () => OnCaptureActive?.Invoke(false);
        _field.OnCapture = (vk, mods) =>
        {
            Settings.HotKeyVk = vk;
            Settings.HotKeyMods = mods;
            Settings.Shortcut = Settings.ShortcutMode.Custom;
            Sync();
            OnChange?.Invoke();
        };

        BuildUi();
        Sync();

        Closed += (_, _) =>
        {
            PersistServerFields();
            OnCaptureActive?.Invoke(false);
            OnClose?.Invoke();
        };
    }

    private void BuildUi()
    {
        var brandRow = new StackPanel { Orientation = Orientation.Horizontal };
        brandRow.Children.Add(new TextBlock
        {
            Text = "▍", FontFamily = Brand.Display, FontSize = 20,
            FontWeight = FontWeights.Bold, Foreground = Brand.MarkerBrush,
        });
        brandRow.Children.Add(new TextBlock
        {
            Text = "Blurt", FontFamily = Brand.Display, FontSize = 20,
            FontWeight = FontWeights.Bold, Foreground = Brand.BoneBrush,
            Margin = new Thickness(6, 0, 0, 0),
        });

        var title = Heading("Settings", 27);
        var subtitle = Body("How you summon Blurt, and where your voice goes.",
            Brand.Solid(Brand.Bone, 0.8), 14);

        // ── card 01: shortcut ──────────────────────────────────
        var options = new (string, Settings.ShortcutMode)[]
        {
            ("Double-tap Ctrl", Settings.ShortcutMode.DoubleTap),
            ("Ctrl + Alt + Space", Settings.ShortcutMode.CtrlAltSpace),
            ("Custom shortcut", Settings.ShortcutMode.Custom),
            ("No shortcut  (tray only)", Settings.ShortcutMode.Off),
        };
        var shortcutRows = new List<UIElement>();
        foreach (var (name, mode) in options)
        {
            var radio = Radio(name, "shortcut");
            radio.Checked += (_, _) => PickShortcut(mode);
            _shortcutRadios[mode] = radio;
            shortcutRows.Add(radio);
            if (mode == Settings.ShortcutMode.Custom)
            {
                var indent = new StackPanel { Orientation = Orientation.Horizontal, Margin = new Thickness(24, 0, 0, 0) };
                indent.Children.Add(_field);
                shortcutRows.Add(indent);
            }
        }
        shortcutRows.Add(new TextBlock
        {
            Text = "Click the well, then press a combo. Esc cancels.",
            FontFamily = Brand.Mono, FontSize = 11, Foreground = Brand.BoneDimBrush,
        });
        var shortcutCard = MakeCard("01", "Shortcut",
            "How you summon Blurt from anywhere.", shortcutRows.ToArray());

        // ── card 02: server ────────────────────────────────────
        _serverField.LostFocus += (_, _) => PersistServerFields();
        _tokenField.LostFocus += (_, _) => PersistServerFields();
        var serverCard = MakeCard("02", "Server",
            "Your own transcription box — audio streams here and nowhere else.",
            new UIElement[] { FieldLabel("WebSocket URL"), _serverField,
                              FieldLabel("Auth token (blank = none)"), _tokenField });

        // ── card 03: insertion ─────────────────────────────────
        var insertRows = new (string, string)[]
        {
            ("Paste  (fast, uses the clipboard)", "paste"),
            ("Type  (slower, works in terminals)", "type"),
        };
        var insertViews = new List<UIElement>();
        foreach (var (name, mode) in insertRows)
        {
            var radio = Radio(name, "insert");
            radio.Checked += (_, _) => PickInsert(mode);
            _insertRadios[mode] = radio;
            insertViews.Add(radio);
        }
        var insertCard = MakeCard("03", "Insertion",
            "How the finished text lands in the focused field.", insertViews.ToArray());

        var stack = new StackPanel { Margin = new Thickness(28) };
        void Add(UIElement e, double gap) { e.SetTopMargin(gap); stack.Children.Add(e); }
        brandRow.SetTopMargin(0); stack.Children.Add(brandRow);
        Add(title, 10);
        Add(subtitle, 8);
        Add(shortcutCard, 22);
        Add(serverCard, 16);
        Add(insertCard, 16);
        Content = stack;
    }

    /// Reflect Settings into the radios and the capture well.
    private void Sync()
    {
        _syncing = true;
        var mode = Settings.Shortcut;
        foreach (var (m, radio) in _shortcutRadios) radio.IsChecked = m == mode;
        _field.IsActive = mode == Settings.ShortcutMode.Custom;
        _field.Refresh();
        foreach (var (m, radio) in _insertRadios) radio.IsChecked = m == Settings.InjectMode;
        _syncing = false;
    }

    private void PickShortcut(Settings.ShortcutMode mode)
    {
        if (_syncing) return;
        Settings.Shortcut = mode;
        Sync();
        OnChange?.Invoke();
        if (mode == Settings.ShortcutMode.Custom) _field.BeginCapture();
    }

    private void PickInsert(string mode)
    {
        if (_syncing) return;
        Settings.InjectMode = mode;
    }

    private void PersistServerFields()
    {
        var url = _serverField.Text.Trim();
        var moved = url != Settings.ServerUrl;
        Settings.ServerUrl = url;
        Settings.AuthToken = _tokenField.Text.Trim();
        if (moved) OnServerChanged?.Invoke();
    }

    // ── view helpers (mirrored from Onboarding.cs) ───────────
    private static TextBlock Heading(string s, double size) => new()
    {
        Text = s, FontFamily = Brand.Display, FontSize = size,
        FontWeight = FontWeights.Bold, Foreground = Brand.BoneBrush, TextWrapping = TextWrapping.Wrap,
    };

    private static TextBlock Body(string s, Brush color, double size) => new()
    {
        Text = s, FontFamily = Brand.Display, FontSize = size, Foreground = color,
        TextWrapping = TextWrapping.Wrap, LineHeight = size * 1.45,
    };

    private static TextBlock FieldLabel(string s) => new()
    {
        Text = s, FontFamily = Brand.Mono, FontSize = 11,
        Foreground = Brand.BoneDimBrush, Margin = new Thickness(0, 10, 0, 4),
    };

    private static RadioButton Radio(string text, string group) => new()
    {
        Content = text, GroupName = group, Foreground = Brand.BoneBrush,
        FontFamily = Brand.Display, FontSize = 14,
    };

    private static TextBox InputField(string value, string placeholder) => new()
    {
        Text = value, FontFamily = Brand.Mono, FontSize = 13,
        Padding = new Thickness(8, 6, 8, 6), Background = Brand.Ink950Brush,
        Foreground = Brand.BoneBrush, CaretBrush = Brand.MarkerBrush,
        BorderBrush = Brand.Ink700Brush, BorderThickness = new Thickness(1), ToolTip = placeholder,
    };

    private static Border MakeCard(string index, string title, string body, UIElement[] rows)
    {
        var left = new StackPanel { Orientation = Orientation.Horizontal };
        left.Children.Add(new TextBlock
        {
            Text = index, FontFamily = Brand.Mono, FontSize = 13, FontWeight = FontWeights.Medium,
            Foreground = Brand.MarkerBrush, VerticalAlignment = VerticalAlignment.Center,
        });
        left.Children.Add(new TextBlock
        {
            Text = title, FontFamily = Brand.Display, FontSize = 18, FontWeight = FontWeights.Bold,
            Foreground = Brand.BoneBrush, Margin = new Thickness(8, 0, 0, 0),
            VerticalAlignment = VerticalAlignment.Center,
        });

        var inner = new StackPanel();
        left.SetTopMargin(0);
        inner.Children.Add(left);
        var bodyLbl = Body(body, Brand.Solid(Brand.Bone, 0.72), 13);
        bodyLbl.SetTopMargin(10);
        inner.Children.Add(bodyLbl);
        foreach (var r in rows) { r.SetTopMargin(12); inner.Children.Add(r); }

        return new Border
        {
            Background = Brand.Ink900Brush, CornerRadius = new CornerRadius(16),
            BorderBrush = Brand.Ink700Brush, BorderThickness = new Thickness(1),
            Padding = new Thickness(20), Child = inner,
        };
    }

    /// A click-to-record well: click it, press a combo, and it reports the virtual
    /// key + MOD_* modifiers. Esc cancels. Bare keys (or shift-only combos) are
    /// rejected — they'd swallow normal typing as a global hotkey — except function
    /// keys. The twin of the Mac client's ShortcutCaptureField (an NSView).
    private sealed class ShortcutCaptureField : Border
    {
        public Action? OnBegin;
        public Action? OnEnd;
        public Action<uint, uint>? OnCapture;

        private readonly TextBlock _label;
        private bool _capturing;
        private bool _active = true;

        public bool IsActive
        {
            get => _active;
            set { _active = value; Focusable = value; Opacity = value ? 1 : 0.35; }
        }

        public ShortcutCaptureField()
        {
            CornerRadius = new CornerRadius(8);
            BorderThickness = new Thickness(1);
            Background = Brand.Solid(Brand.Ink850);
            MinWidth = 200;
            Height = 34;
            Focusable = true;
            FocusVisualStyle = null;
            _label = new TextBlock
            {
                FontFamily = Brand.Mono, FontSize = 13, FontWeight = FontWeights.Medium,
                HorizontalAlignment = HorizontalAlignment.Center,
                VerticalAlignment = VerticalAlignment.Center,
            };
            Child = _label;
            Refresh();

            PreviewMouseDown += (_, e) => { if (_active) { Keyboard.Focus(this); e.Handled = true; } };
            GotKeyboardFocus += (_, _) => { if (_active) BeginCapture(); };
            LostKeyboardFocus += (_, _) => EndCapture();
            PreviewKeyDown += (_, e) => Handle(e);
        }

        public void BeginCapture()
        {
            if (!_active) return;
            if (!IsKeyboardFocused) { Keyboard.Focus(this); return; }
            _capturing = true;
            Refresh();
            OnBegin?.Invoke();
        }

        public void EndCapture()
        {
            if (!_capturing) return;
            _capturing = false;
            Refresh();
            OnEnd?.Invoke();
        }

        public void Refresh()
        {
            BorderBrush = _capturing ? Brand.MarkerBrush : Brand.Ink600Brush;
            _label.Foreground = _capturing ? Brand.MarkerBrush : Brand.BoneBrush;
            _label.Text = _capturing
                ? "type shortcut…"
                : ShortcutLabel.Describe(Settings.HotKeyVk, Settings.HotKeyMods);
        }

        private static bool IsModifierKey(Key k) => k is
            Key.LeftCtrl or Key.RightCtrl or Key.LeftAlt or Key.RightAlt or
            Key.LeftShift or Key.RightShift or Key.LWin or Key.RWin or Key.System;

        private void Handle(KeyEventArgs e)
        {
            if (!_capturing) return;
            e.Handled = true;
            var key = e.Key == Key.System ? e.SystemKey : e.Key;

            if (IsModifierKey(key))
            {
                // Only modifiers held so far: show them live, wait for a real key.
                var m = ShortcutLabel.ModsFromWpf(Keyboard.Modifiers);
                _label.Text = m == 0 ? "type shortcut…" : ShortcutLabel.Modifiers(m).TrimEnd('+');
                return;
            }
            if (key == Key.Escape && Keyboard.Modifiers == ModifierKeys.None)
            {
                Keyboard.ClearFocus();
                return;
            }
            var mods = ShortcutLabel.ModsFromWpf(Keyboard.Modifiers);
            var vk = (uint)KeyInterop.VirtualKeyFromKey(key);
            // Reject bare / shift-only combos (they'd hijack normal typing) unless it's a function key.
            if ((mods & ~ShortcutLabel.MOD_SHIFT) == 0 && !ShortcutLabel.IsFunctionKey(vk))
            {
                SystemSounds.Beep.Play();
                return;
            }
            OnCapture?.Invoke(vk, mods);
            Keyboard.ClearFocus();
        }
    }
}
