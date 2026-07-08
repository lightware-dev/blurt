using System.Drawing;
using System.Windows.Forms;
using System.Windows.Threading;
// Alias the two WPF types we need by full name so we can skip `using System.Windows;`
// — importing it alongside WinForms + System.Drawing makes Point/Size/Color ambiguous.
using Application = System.Windows.Application;
using MessageBox = System.Windows.MessageBox;

namespace Blurt;

/// The app's brain, the twin of the Mac client's AppDelegate.swift: owns the tray
/// icon + menu, wires audio → socket → HUD → text injection together, and runs the
/// dictation state machine. Toggled by the global Ctrl+Alt+Space hotkey.
internal sealed class BlurtApp : IDisposable
{
    private readonly Application _app;
    private readonly Dispatcher _ui;

    private readonly AudioCapture _audio = new();
    private readonly DictationClient _client = new();
    private readonly Hud _hud = new();

    private NotifyIcon? _tray;
    private ToolStripMenuItem? _injectItem;
    private ToolStripMenuItem? _startupItem;
    private HotKey? _hotKey;
    private Icon? _currentIcon;

    private bool _recording;
    private Onboarding? _onboarding;

    public BlurtApp(Application app)
    {
        _app = app;
        _ui = app.Dispatcher;
    }

    public void Start()
    {
        SetupTray();
        Wire();
        SetupHotKey();

        // First-run setup until the user has seen it once. Unlike macOS there's no
        // Accessibility gate to clear — text injection works without any grant.
        if (!Settings.DidOnboard) ShowOnboarding();
    }

    // MARK: tray

    private void SetupTray()
    {
        var menu = new ContextMenuStrip();
        Add(menu, "Start / Stop Blurting  (Ctrl+Alt+Space)", (_, _) => Toggle());
        menu.Items.Add(new ToolStripSeparator());
        Add(menu, "Setup…", (_, _) => ShowOnboarding());
        Add(menu, "Set Server URL…", (_, _) => SetServer());
        Add(menu, "Set Auth Token…", (_, _) => SetToken());

        _injectItem = new ToolStripMenuItem("Insert via Typing (not Paste)", null, (_, _) => ToggleInject())
        { Checked = Settings.InjectMode == "type", CheckOnClick = false };
        menu.Items.Add(_injectItem);

        _startupItem = new ToolStripMenuItem("Start at Login", null, (_, _) => ToggleStartup())
        { Checked = Settings.StartAtLogin, CheckOnClick = false };
        menu.Items.Add(_startupItem);

        menu.Items.Add(new ToolStripSeparator());
        Add(menu, "Quit Blurt", (_, _) => Quit());

        _currentIcon = IconFactory.Tray(false);
        _tray = new NotifyIcon
        {
            Icon = _currentIcon,
            Text = "Blurt",
            Visible = true,
            ContextMenuStrip = menu,
        };
        // Left double-click toggles, mirroring a quick tap on the Mac menu item.
        _tray.DoubleClick += (_, _) => Toggle();
    }

    private static void Add(ContextMenuStrip menu, string title, EventHandler onClick)
        => menu.Items.Add(new ToolStripMenuItem(title, null, onClick));

    private void UpdateIcon()
    {
        if (_tray is null) return;
        var next = IconFactory.Tray(_recording);
        _tray.Icon = next;
        _currentIcon?.Dispose();
        _currentIcon = next;
    }

    // MARK: wiring

    private void Wire()
    {
        _audio.OnFrame = (data, count) => _client.SendAudio(data, count);

        _client.OnPartial = text => _ui.Invoke(() => _hud.Show(text));
        _client.OnFinal = text => _ui.Invoke(() =>
        {
            _hud.Hide();
            _client.Close();
            if (!string.IsNullOrEmpty(text)) TextInjector.Inject(text);
        });
        _client.OnError = msg => _ui.Invoke(() =>
        {
            _hud.Hide();
            ForceStop();
            Notify("Connection error", msg);
        });
    }

    private void SetupHotKey()
    {
        _hotKey = HotKey.CtrlAltSpace(() => _ui.Invoke(Toggle));
        if (!_hotKey.Registered)
            Notify("Hotkey unavailable", "Could not register Ctrl+Alt+Space. Another app may own it.");
    }

    // MARK: dictation state machine

    private void Toggle()
    {
        if (_recording) StopRecording();
        else StartRecording();
    }

    private void StartRecording()
    {
        if (_recording) return;
        _client.ConnectAndStart();
        try
        {
            _audio.Start();
        }
        catch (Exception ex)
        {
            _client.Close();
            Notify("Microphone error", ex.Message);
            return;
        }
        _recording = true;
        UpdateIcon();
        _hud.Show(""); // empty → HUD shows its faded "Listening…" placeholder
    }

    private void StopRecording()
    {
        if (!_recording) return;
        _recording = false;
        UpdateIcon();
        _audio.Stop();
        _client.Stop(); // server replies with {final}; OnFinal injects + closes
    }

    private void ForceStop()
    {
        _recording = false;
        UpdateIcon();
        _audio.Stop();
        _client.Close();
    }

    // MARK: menu actions

    private void ShowOnboarding()
    {
        if (_onboarding is not null) { _onboarding.Activate(); return; }
        var win = new Onboarding();
        win.OnClose = () => _onboarding = null;
        _onboarding = win;
        _app.MainWindow = win;
        win.Show();
        win.Activate();
    }

    private void SetServer()
    {
        var v = PromptWindow.Ask("Server WebSocket URL", Settings.ServerUrl, "e.g. wss://192.168.1.50:25878/ws");
        if (v is not null) Settings.ServerUrl = v;
    }

    private void SetToken()
    {
        var v = PromptWindow.Ask("Auth token (blank = none)", Settings.AuthToken);
        if (v is not null) Settings.AuthToken = v;
    }

    private void ToggleInject()
    {
        Settings.InjectMode = Settings.InjectMode == "type" ? "paste" : "type";
        if (_injectItem is not null) _injectItem.Checked = Settings.InjectMode == "type";
    }

    private void ToggleStartup()
    {
        var enabled = !Settings.StartAtLogin;
        Settings.StartAtLogin = enabled;
        StartupRegistration.Apply(enabled);
        if (_startupItem is not null) _startupItem.Checked = enabled;
    }

    private void Quit()
    {
        Dispose();
        _app.Shutdown();
    }

    // MARK: helpers

    private void Notify(string title, string body)
    {
        // A balloon tip stays out of the way during dictation (vs. a modal that
        // would steal focus from the field being typed into).
        if (_tray is not null)
        {
            _tray.BalloonTipTitle = title;
            _tray.BalloonTipText = body;
            _tray.ShowBalloonTip(4000);
        }
        else
        {
            MessageBox.Show(body, title);
        }
    }

    public void Dispose()
    {
        _hotKey?.Dispose();
        _audio.Dispose();
        _client.Close();
        if (_tray is not null) { _tray.Visible = false; _tray.Dispose(); _tray = null; }
        _currentIcon?.Dispose();
    }
}
