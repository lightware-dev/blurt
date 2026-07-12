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
    private ToolStripMenuItem? _toggleItem;
    private ToolStripMenuItem? _startupItem;
    private ToolStripMenuItem? _copyLastItem;
    private HotKey? _hotKey;
    private ModifierDoubleTap? _doubleTap;
    // Registered only while recording so it never swallows Esc globally otherwise.
    private HotKey? _cancelKey;
    private Icon? _currentIcon;

    private bool _recording;
    private Onboarding? _onboarding;
    private SettingsWindow? _settings;

    // The most recent finalized dictation, kept in memory only so it can be
    // recovered from the menu when it landed somewhere unexpected. Never persisted.
    private string? _lastDictation;

    public BlurtApp(Application app)
    {
        _app = app;
        _ui = app.Dispatcher;
    }

    public void Start()
    {
        // Sweep away the previous version's exe left behind by a prior self-update.
        // Off the UI thread since it may briefly retry while the old process exits.
        Task.Run(Updater.CleanupOldExe);

        SetupTray();
        Wire();
        ApplyShortcut();

        // First-run setup until the user has seen it once. Unlike macOS there's no
        // Accessibility gate to clear — text injection works without any grant.
        if (!Settings.DidOnboard) ShowOnboarding();

        // Quietly check GitHub for a newer release on launch; only prompts if one
        // exists. Skipped under the debugger so dev runs aren't nagged to "update".
        if (!System.Diagnostics.Debugger.IsAttached)
            _ = CheckForUpdatesAsync(silent: true);
    }

    // MARK: tray

    private void SetupTray()
    {
        var menu = new ContextMenuStrip();
        // Label gets the active shortcut appended by ApplyShortcut().
        _toggleItem = new ToolStripMenuItem("Start / Stop Blurting", null, (_, _) => Toggle());
        menu.Items.Add(_toggleItem);
        _copyLastItem = new ToolStripMenuItem("Copy Last Dictation", null, (_, _) => CopyLastDictation())
        { Enabled = false };
        menu.Items.Add(_copyLastItem);
        menu.Items.Add(new ToolStripSeparator());
        Add(menu, "Settings…", (_, _) => ShowSettings());
        Add(menu, "Setup…", (_, _) => ShowOnboarding());

        _startupItem = new ToolStripMenuItem("Start at Login", null, (_, _) => ToggleStartup())
        { Checked = Settings.StartAtLogin, CheckOnClick = false };
        menu.Items.Add(_startupItem);

        menu.Items.Add(new ToolStripSeparator());
        Add(menu, "Check for Updates…", (_, _) => _ = CheckForUpdatesAsync(silent: false));
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
        // Non-blocking (InvokeAsync): the spectrum fires on the audio thread ~10×/s
        // and mustn't stall mic capture waiting on the UI. Mirrors DispatchQueue.main.async.
        _audio.OnSpectrum = bands => _ui.InvokeAsync(() => _hud.Spectrum(bands));

        // Skip a partial that arrives after teardown has begun — it would resurrect
        // the HUD after a "Cancelled" flash (matches the Swift `closing` guard).
        _client.OnPartial = text => _ui.Invoke(() =>
        {
            if (_client.Closing) return;
            _hud.Show(text);
        });
        _client.OnFinal = text => _ui.Invoke(() =>
        {
            _hud.Hide();
            _client.Close();
            if (!string.IsNullOrEmpty(text))
            {
                _lastDictation = text;
                if (_copyLastItem is not null) _copyLastItem.Enabled = true;
                TextInjector.Inject(text);
            }
        });
        _client.OnStatus = (state, detail) => _ui.Invoke(() =>
        {
            // The server reports a fatal decode failure (e.g. a wedged CUDA context)
            // as {status: error}. Surface it instead of leaving the HUD stuck on
            // "Listening…" with no text ever arriving.
            if (state != "error") return;
            ForceStop();
            _hud.Hide();
            var first = detail?.Split('\n', 2)[0];
            Notify("Dictation server error",
                string.IsNullOrEmpty(first)
                    ? "The server could not transcribe. Try again, and restart the server if it persists."
                    : first);
        });
        _client.OnError = msg => _ui.Invoke(() =>
        {
            _hud.Hide();
            ForceStop();
            Notify("Connection error", msg);
        });
    }

    /// (Re)arm the dictation trigger from Settings.Shortcut, and reflect the active
    /// shortcut in the tray toggle label. The twin of the Mac client's applyShortcut().
    private void ApplyShortcut()
    {
        _hotKey?.Dispose();
        _hotKey = null;
        _doubleTap?.Dispose();
        _doubleTap = null;

        switch (Settings.Shortcut)
        {
            case Settings.ShortcutMode.DoubleTap:
                // The low-level hook fires on the UI thread; post async so we never
                // stall the system-wide input pipeline while (re)connecting.
                _doubleTap = new ModifierDoubleTap(() => _ui.InvokeAsync(Toggle));
                break;
            case Settings.ShortcutMode.CtrlAltSpace:
                _hotKey = HotKey.CtrlAltSpace(() => _ui.Invoke(Toggle));
                break;
            case Settings.ShortcutMode.Custom:
                _hotKey = new HotKey(Settings.HotKeyMods, Settings.HotKeyVk, () => _ui.Invoke(Toggle));
                break;
            case Settings.ShortcutMode.Off:
                break;
        }

        if (_hotKey is { Registered: false })
            Notify("Hotkey unavailable", $"Could not register {ShortcutLabel.Current()}. Another app may own it.");

        var label = ShortcutLabel.Current();
        if (_toggleItem is not null)
            _toggleItem.Text = label.Length == 0
                ? "Start / Stop Blurting"
                : $"Start / Stop Blurting  ({label})";
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
        // Esc discards the in-flight dictation. Registered per-session so we only
        // capture Esc while actually listening.
        _cancelKey = HotKey.Escape(() => _ui.Invoke(Cancel));
        _hud.Show(""); // empty → HUD shows its faded "Listening…" placeholder
    }

    private void StopRecording()
    {
        if (!_recording) return;
        _recording = false;
        DisposeCancelKey();
        UpdateIcon();
        _audio.Stop();
        _client.Stop(); // server replies with {final}; OnFinal injects + closes
    }

    /// Discard the in-flight dictation: stop without asking the server to finalize,
    /// so nothing gets injected. Bound to Esc.
    private void Cancel()
    {
        if (!_recording) return;
        ForceStop();
        _hud.Flash("Cancelled");
    }

    private void ForceStop()
    {
        _recording = false;
        DisposeCancelKey();
        UpdateIcon();
        _audio.Stop();
        _client.Close();
    }

    private void DisposeCancelKey()
    {
        _cancelKey?.Dispose();
        _cancelKey = null;
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

    /// Copy the last finalized dictation to the clipboard — a safety net for when
    /// the text got injected into the wrong field. Held in memory only.
    private void CopyLastDictation()
    {
        if (string.IsNullOrEmpty(_lastDictation)) return;
        try { System.Windows.Clipboard.SetText(_lastDictation); } catch { }
    }

    private void ShowSettings()
    {
        if (_settings is not null) { _settings.Activate(); return; }
        var win = new SettingsWindow
        {
            OnChange = ApplyShortcut,
            // Suspend our own triggers while a custom combo is being recorded, so the
            // active hotkey/double-tap can't swallow the keys being typed into the well.
            OnCaptureActive = capturing =>
            {
                if (capturing)
                {
                    _hotKey?.Dispose(); _hotKey = null;
                    _doubleTap?.Dispose(); _doubleTap = null;
                }
                else ApplyShortcut();
            },
            OnClose = () => _settings = null,
        };
        _settings = win;
        win.Show();
        win.Activate();
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

    // MARK: updates

    /// Check GitHub for a newer release; if one exists, prompt, download, then prompt
    /// to restart into it. `silent` startup checks stay quiet unless there's an update
    /// (and swallow network errors); a manual check always gives feedback. All the
    /// dialogs are marshalled onto the UI thread since the awaits resume on a pool thread.
    private async Task CheckForUpdatesAsync(bool silent)
    {
        Updater.Release? release;
        try
        {
            release = await Updater.CheckAsync();
        }
        catch (Exception ex)
        {
            if (!silent) _ui.Invoke(() => Info($"Couldn't check for updates:\n{ex.Message}"));
            return;
        }

        if (release is null)
        {
            if (!silent) _ui.Invoke(() => Info($"You're up to date (v{Updater.CurrentVersion})."));
            return;
        }

        var accept = _ui.Invoke(() => Ask("Update available",
            $"Blurt {release.Tag} is available — you have v{Updater.CurrentVersion}.\n\nDownload and install it now?"));
        if (!accept) return;

        _ui.Invoke(() => Notify("Blurt", "Downloading update…"));

        string newExe;
        try
        {
            newExe = await Updater.DownloadAsync(release);
        }
        catch (Exception ex)
        {
            _ui.Invoke(() => Info($"The update download failed:\n{ex.Message}"));
            return;
        }

        var restart = _ui.Invoke(() => Ask("Update ready",
            "The update is downloaded. Restart Blurt now to finish installing it?"));
        if (!restart) return;

        _ui.Invoke(() => ApplyUpdateAndRestart(newExe));
    }

    /// Swap in the new exe and relaunch. Frees the hotkey + tray first so the fresh
    /// instance can claim them, then exits this one.
    private void ApplyUpdateAndRestart(string newExe)
    {
        string exePath;
        try
        {
            exePath = Updater.ApplyUpdate(newExe);
        }
        catch (Exception ex)
        {
            Info($"Couldn't install the update:\n{ex.Message}\n\n" +
                 $"You can download it manually from:\n{Updater.ReleasesUrl}");
            return;
        }

        Dispose(); // release the global hotkey and tray icon before the new exe starts
        try { Updater.Launch(exePath); } catch { /* the user can relaunch Blurt manually */ }
        _app.Shutdown();
    }

    // MARK: helpers

    /// A blocking Yes/No prompt; returns true on Yes.
    private static bool Ask(string title, string body) =>
        MessageBox.Show(body, title,
            System.Windows.MessageBoxButton.YesNo, System.Windows.MessageBoxImage.Question)
            == System.Windows.MessageBoxResult.Yes;

    /// A simple informational dialog.
    private static void Info(string body) =>
        MessageBox.Show(body, "Blurt",
            System.Windows.MessageBoxButton.OK, System.Windows.MessageBoxImage.Information);

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
        _doubleTap?.Dispose();
        DisposeCancelKey();
        _audio.Dispose();
        _client.Close();
        if (_tray is not null) { _tray.Visible = false; _tray.Dispose(); _tray = null; }
        _currentIcon?.Dispose();
    }
}
