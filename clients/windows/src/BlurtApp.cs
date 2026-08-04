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
    // Shown only while the server is known to be down (see SetServerReachable).
    private ToolStripMenuItem? _serverDownItem;
    private ToolStripSeparator? _serverDownSeparator;
    private ToolStripMenuItem? _startupItem;
    private ToolStripMenuItem? _copyLastItem;
    private HotKey? _hotKey;
    private ModifierDoubleTap? _doubleTap;
    // Registered only while recording so it never swallows Esc globally otherwise.
    private HotKey? _cancelKey;
    private Icon? _currentIcon;

    private bool _recording;
    // True while a certificate pre-flight is in flight, so a second trigger can't
    // stack another probe (and another dialog) on top of the first.
    private bool _preflighting;
    private Onboarding? _onboarding;
    private SettingsWindow? _settings;

    // The most recent finalized dictation, kept in memory only so it can be
    // recovered from the menu when it landed somewhere unexpected. Never persisted.
    private string? _lastDictation;

    // Live protocol state for the HUD: the latest structured partial, whether the
    // server's VAD currently hears speech, and whether the model is still loading.
    private string _partialCommitted = "";
    private string _partialLive = "";
    private bool _serverHearing;
    private bool _serverLoading;
    // Whether the server has said anything on this connection. Until it has, the
    // HUD must not claim to be listening: the mic is live, but nothing is
    // receiving it.
    private bool _serverAcked;
    // The last thing we learned about whether the server answers at all — from a
    // certificate pre-flight or a dictation. Null until something has tried.
    // Drives the tray, so a dead daemon is visible *before* the hotkey is
    // pressed and not only after a dictation has been spoken into the void.
    private bool? _serverReachable;

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

        // WPF windows have to be built on the dispatcher thread; the trust check
        // runs off it, so it reaches the dialog through here.
        CertTrust.Confirm = decision => _ui.Invoke(() => TrustWindow.Ask(decision));

        SetupTray();
        Wire();
        ApplyShortcut();

        // First-run setup until the user has seen it once. Unlike macOS there's no
        // Accessibility gate to clear — text injection works without any grant.
        if (!Settings.DidOnboard) ShowOnboarding();   // certificate check follows on close
        else CheckServerCertificate();

        // Quietly check GitHub for a newer release on launch; only prompts if one
        // exists. Skipped under the debugger so dev runs aren't nagged to "update".
        if (!System.Diagnostics.Debugger.IsAttached)
            _ = CheckForUpdatesAsync(silent: true);
    }

    // MARK: tray

    private void SetupTray()
    {
        var menu = new ContextMenuStrip();
        // First, above everything, and hidden unless it applies: if the server is
        // down there is nothing else worth reading in here.
        _serverDownItem = new ToolStripMenuItem("") { Enabled = false, Visible = false };
        menu.Items.Add(_serverDownItem);
        _serverDownSeparator = new ToolStripSeparator { Visible = false };
        menu.Items.Add(_serverDownSeparator);
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
        // The hover text carries what the icon can't: whether the server is
        // there. Truncated because NotifyIcon.Text throws past 63 characters, and
        // a long enough hostname would get there.
        var tip = _serverReachable == false ? $"Blurt — can't reach {ServerLabel()}" : "Blurt";
        _tray.Text = tip.Length <= 63 ? tip : tip[..63];
    }

    private void UpdateServerDownItem()
    {
        var down = _serverReachable == false;
        if (_serverDownItem is not null)
        {
            _serverDownItem.Text = $"Can't reach {ServerLabel()}";
            _serverDownItem.Visible = down;
        }
        if (_serverDownSeparator is not null) _serverDownSeparator.Visible = down;
    }

    /// Record whether the server answers, and reflect it in the tray: the hover
    /// text plus a line naming the host that isn't responding.
    private void SetServerReachable(bool reachable)
    {
        if (_serverReachable == reachable) return;
        _serverReachable = reachable;
        UpdateIcon();
        UpdateServerDownItem();
    }

    /// Fold what a pre-flight learned into the tray's picture of the server. The
    /// probe already opens a connection, so it knows whether anything is
    /// listening — dropping that on the floor is why a stopped daemon stayed
    /// invisible until a dictation had already been lost to it.
    private void NoteProbe(CertTrust.Outcome outcome)
    {
        if (!CertTrust.ProbesReachability(Settings.ServerUrl)) return;
        SetServerReachable(outcome != CertTrust.Outcome.Unreachable);
    }

    /// `host:port` for the configured server — what to name in a message about
    /// it failing, rather than the whole `wss://…/ws` URL.
    private static string ServerLabel()
    {
        var url = Settings.ServerUrl.Trim();
        return Uri.TryCreate(url, UriKind.Absolute, out var parsed)
            ? $"{parsed.Host}:{CertTrust.Port(parsed)}"
            : url;
    }

    // MARK: wiring

    private void Wire()
    {
        _audio.OnFrame = (data, count) => _client.SendAudio(data, count);
        // Non-blocking (InvokeAsync): the spectrum fires on the audio thread ~10×/s
        // and mustn't stall mic capture waiting on the UI. Mirrors DispatchQueue.main.async.
        _audio.OnSpectrum = bands => _ui.InvokeAsync(() => _hud.Spectrum(bands));

        // The server's first word. Only now is the HUD entitled to say it's
        // listening, and only now do we know the daemon is up.
        _client.OnConnected = () => _ui.Invoke(() =>
        {
            if (_client.Closing) return;
            _serverAcked = true;
            SetServerReachable(true);
            RenderHud();
        });

        // Skip a partial that arrives after teardown has begun — it would resurrect
        // the HUD after a "Cancelled" flash (matches the Swift `closing` guard).
        _client.OnPartial = (committed, live) => _ui.Invoke(() =>
        {
            if (_client.Closing) return;
            _partialCommitted = committed;
            _partialLive = live;
            _serverLoading = false;   // text proves the model is up
            RenderHud();
        });
        // Server-side VAD: before any text arrives, flip the placeholder to
        // "Hearing you…" — end-to-end confirmation that mic → network → server
        // is alive (the waveform only proves the local mic works).
        _client.OnVad = speech => _ui.Invoke(() =>
        {
            if (_client.Closing) return;
            _serverHearing = speech;
            // `info` is a one-shot snapshot taken at connect; if it said
            // "loading", nothing else would ever clear it and the placeholder
            // would outrank "Hearing you…" for the whole dictation. Any event
            // from the pipeline proves loading is done.
            _serverLoading = false;
            // The placeholder only renders before the first word arrives, so
            // the meter is what carries this state for the rest of the
            // dictation.
            _hud.Hearing(speech);
            RenderHud();
        });
        _client.OnInfo = (state, _) => _ui.Invoke(() =>
        {
            if (_client.Closing) return;
            _serverLoading = state == "loading";
            RenderHud();
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
        _client.OnUnreachable = detail => _ui.Invoke(() => ConnectionFailed(detail, unreachable: true));
        _client.OnError = msg => _ui.Invoke(() => ConnectionFailed(msg, unreachable: false));
    }

    /// Every way a dictation's connection can die ends here: stop recording, then
    /// say what happened. `unreachable` means the server never spoke at all,
    /// which has one overwhelmingly likely cause worth naming outright.
    private void ConnectionFailed(string detail, bool unreachable)
    {
        ForceStop();
        // A refused certificate arrives here as an opaque TLS failure. Say what
        // really happened and offer to pin it — the fingerprint is already in
        // hand, so this needs no second handshake. Reaching this means the
        // certificate changed since the pre-flight; the next trigger connects.
        if (_client.CertRejection is { } rejection)
        {
            _hud.Hide();
            // Either way this dictation is gone, so say so rather than
            // letting the HUD vanish with no explanation.
            if (CertTrust.PromptAndPin(rejection))
                Notify("Certificate trusted", "Press the shortcut again to dictate.");
            else
                Notify("Connection refused",
                    $"The certificate for {rejection.Host} isn't trusted, so Blurt didn't connect.");
            return;
        }
        if (!unreachable)
        {
            _hud.Hide();
            Notify("Connection error", detail);
            return;
        }
        SetServerReachable(false);
        // Put the diagnosis in the HUD before the balloon. The HUD is the one
        // surface guaranteed to be in front of the user, and it's where they were
        // already looking for their words.
        _hud.Flash("Can't reach the server", TimeSpan.FromSeconds(2.5));
        var shortcut = ShortcutLabel.Current();
        Notify("Can't reach the Blurt server",
            $"Nothing answered at {ServerLabel()}, so this dictation was lost. Check that blurtd " +
            $"is running there, then press {(shortcut.Length == 0 ? "the shortcut" : shortcut)} again.");
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

    /// One render path for the live HUD: structured partial when there's text,
    /// otherwise a placeholder that reflects what the server is actually doing.
    private void RenderHud()
    {
        if (!_recording) return;
        // "Connecting…" until the server has actually answered. Saying
        // "Listening…" over a socket nobody is on the other end of is the whole
        // reason a stopped daemon could go unnoticed for a minute at a time.
        var placeholder = !_serverAcked  ? "Connecting…"
                        : _serverLoading ? "Loading model…"
                        : _serverHearing ? "Hearing you…"
                        : "Listening…";
        _hud.Show(_partialCommitted, _partialLive, placeholder);
    }

    // MARK: dictation state machine

    private void Toggle()
    {
        if (_preflighting) return;
        if (_recording) StopRecording();
        else StartRecording();
    }

    /// Settle the server's certificate *before* the HUD and the mic come up. A
    /// trust dialog over a live "Listening…" HUD would swallow whatever the user
    /// said while it was on screen, so the first dictation against an unknown
    /// server pays a handshake here instead of losing itself. Only runs when the
    /// host is still unsettled — the steady state goes straight to BeginRecording.
    private void StartRecording()
    {
        if (_recording) return;
        if (!CertTrust.NeedsCheck(Settings.ServerUrl)) { BeginRecording(); return; }
        _preflighting = true;
        _ = PreflightThenRecordAsync();
    }

    private async Task PreflightThenRecordAsync()
    {
        var outcome = CertTrust.Outcome.Unreachable;
        try
        {
            outcome = await CertTrust.PreflightAsync(Settings.ServerUrl).ConfigureAwait(false);
        }
        catch { /* treated as unreachable — the connection attempt reports the real error */ }
        _ui.Invoke(() =>
        {
            _preflighting = false;
            NoteProbe(outcome);
            // Unreachable falls through to a normal connection attempt, so an
            // offline server still gives the usual error rather than silence.
            if (outcome != CertTrust.Outcome.Refused) BeginRecording();
        });
    }

    private void BeginRecording()
    {
        if (_recording) return;
        _partialCommitted = "";
        _partialLive = "";
        _serverHearing = false;
        _serverLoading = false;
        _serverAcked = false;
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
        RenderHud();          // no text yet → the faded "Connecting…" placeholder
        _hud.Hearing(false);  // muted until the server reports it hears speech
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
        win.OnClose = () =>
        {
            _onboarding = null;
            CheckServerCertificate();
        };
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
            // A new server means a new certificate: settle it here rather than
            // letting the next dictation discover it.
            OnServerChanged = CheckServerCertificate,
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

    /// Settle trust in the server's TLS certificate now, while nothing is being
    /// dictated, so the first hotkey press isn't what triggers a trust dialog.
    /// Silent unless the certificate is unknown or has changed.
    ///
    /// A global hotkey still fires while a modal dialog is up, so this holds
    /// `_preflighting` for the duration — otherwise a trigger during the dialog
    /// would start recording behind it.
    private void CheckServerCertificate()
    {
        _preflighting = true;
        _ = CheckServerCertificateAsync();
    }

    private async Task CheckServerCertificateAsync()
    {
        var outcome = CertTrust.Outcome.Unreachable;
        try { outcome = await CertTrust.PreflightAsync(Settings.ServerUrl).ConfigureAwait(false); }
        catch { /* treated as unreachable; the check itself is silent unless it prompts */ }
        _ui.Invoke(() =>
        {
            _preflighting = false;
            NoteProbe(outcome);
        });
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
