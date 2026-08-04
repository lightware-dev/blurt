using System.IO;
using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace Blurt;

/// WebSocket client to the Parakeet server (see docs/protocol.md), the twin of
/// the Mac client's DictationClient.swift. Sends {start} with a fresh dictation
/// id and the declared audio format, streams PCM16 binary frames, sends
/// {stop}; surfaces info/vad/partial/final/status/error back to the caller.
/// Messages tagged with a stale dictation id (a previous dictation's late
/// final, for example) are dropped. Callbacks fire
/// on a background thread — the app marshals them onto the UI thread.
internal sealed class DictationClient
{
    /// How long the server gets to say hello before we call it unreachable.
    ///
    /// The protocol has the server send `info` the instant the socket opens, so
    /// a live blurtd answers in milliseconds. A dead one, though, does not
    /// reliably *refuse*: a host asleep behind a VPN, a port forwarded to
    /// nothing, a daemon still coming up — all of those swallow the connection
    /// and then say nothing, and ConnectAsync will wait far longer than anyone
    /// keeps talking. That wait used to pass with the mic live and the HUD
    /// reading "Listening…", so the dictation went into a socket that was never
    /// going to answer. Generous enough for a slow link, short enough that
    /// nobody gets through a sentence first. (Mirrors the Swift twin.)
    private static readonly TimeSpan HandshakeTimeout = TimeSpan.FromSeconds(3);

    public Action<string, string>? OnPartial; // (committed, live) — live may still be revised
    public Action<string>? OnFinal;
    public Action<bool>? OnVad;               // server-side VAD: is it hearing speech?
    public Action<string, string>? OnInfo;    // (state "ready|loading", model)
    public Action<string, string?>? OnStatus; // (state, detail)
    public Action<string>? OnError;
    /// The server's first message — the only proof that dictation can actually
    /// happen. Fires once per connection, ahead of the message's own callback.
    public Action? OnConnected;
    /// The server never got as far as speaking: nothing is listening, the host
    /// is unreachable, or it accepted the connection and went quiet. Kept apart
    /// from <see cref="OnError"/> because the cause is specific and the fix is
    /// actionable — start blurtd — where a mid-dictation drop is neither.
    public Action<string>? OnUnreachable;

    /// Why the last handshake was refused, when it was refused over the server's
    /// certificate. A rejected TLS handshake surfaces to <see cref="OnError"/> as
    /// an opaque exception, so the app reads this to say what actually happened —
    /// and to offer to pin the new certificate without a second handshake.
    /// Written on the TLS callback's thread, read on the UI thread.
    public volatile TrustDecision? CertRejection;

    private ClientWebSocket? _socket;
    private CancellationTokenSource? _cts;
    private volatile bool _closing;
    /// Whether anything has come back from the server on this connection. Until
    /// it has, we have no evidence of a blurtd on the other end — TCP and TLS
    /// both succeed against plenty of things that will never transcribe
    /// anything.
    private volatile bool _serverSpoke;
    /// 0/1 via Interlocked: a dying connection can fail several ways at once —
    /// the receive loop, the handshake watchdog, every queued send — and the
    /// user needs to hear about it once.
    private int _reportedFailure;
    // Written on the UI thread by ConnectAndStart, read on the receive-loop
    // thread by Handle — volatile so the reader can't see a stale value.
    private volatile string _dictationId = "";

    /// True once Close()/Cancel has begun tearing the session down. A partial can
    /// already be queued on the UI thread when that happens (e.g. Esc-cancel):
    /// delivering it would resurrect the HUD after the "Cancelled" flash and leave
    /// it stuck on screen, so callers skip late partials while this is set (mirrors
    /// the Swift `guard !self.closing` in DictationClient.swift).
    public bool Closing => _closing;

    // ClientWebSocket forbids concurrent sends; audio frames (every ~100 ms) and
    // the start/stop control messages all funnel through this gate.
    private readonly SemaphoreSlim _sendGate = new(1, 1);

    public void ConnectAndStart()
    {
        if (!Uri.TryCreate(BuildUrl(), UriKind.Absolute, out var url))
        {
            OnError?.Invoke("Bad server URL");
            return;
        }

        _closing = false;
        _serverSpoke = false;
        _reportedFailure = 0;
        _dictationId = Guid.NewGuid().ToString("N");
        _cts = new CancellationTokenSource();
        CertRejection = null;
        var socket = new ClientWebSocket();
        // Validate the server's certificate — silently. A certificate that is
        // system-valid or matches the pin for this host is accepted; anything else
        // drops the connection and records why.
        //
        // Deliberately never prompts: by the time this runs the HUD is up and the
        // mic is live, so a trust dialog here would eat the user's first sentence.
        // CertTrust.PreflightAsync settles trust before recording ever starts.
        var host = url.Host;
        var port = CertTrust.Port(url);
        socket.Options.RemoteCertificateValidationCallback = (_, cert, _, errors) =>
        {
            var decision = CertTrust.Evaluate(cert, host, port, errors);
            if (decision.Kind == TrustKind.Trusted)
            {
                CertTrust.MarkVerified(decision.Key);
                return true;
            }
            CertRejection = decision;
            // So the next dictation settles this up front rather than bringing
            // the mic up and failing the same way again.
            CertTrust.MarkRejected(decision.Key);
            return false;
        };
        _socket = socket;

        _ = RunAsync(socket, url, _cts.Token);
        _ = WatchdogAsync(_cts.Token);
    }

    /// Give up on a server that took the connection — or never even refused it —
    /// and then said nothing. A no-op once its first message has landed.
    private async Task WatchdogAsync(CancellationToken ct)
    {
        try { await Task.Delay(HandshakeTimeout, ct).ConfigureAwait(false); }
        catch (OperationCanceledException) { return; }
        if (_serverSpoke) return;
        ReportFailure($"No reply within {(int)HandshakeTimeout.TotalSeconds} seconds.");
    }

    /// Report a dead connection once, tear the socket down, and route it by
    /// whether the server ever spoke: silence from the start is an unreachable
    /// server, a drop after it spoke is a lost connection.
    private void ReportFailure(string message)
    {
        // A deliberate Close() fails everything in flight; that's not an error.
        if (_closing) return;
        if (Interlocked.Exchange(ref _reportedFailure, 1) != 0) return;
        var spoke = _serverSpoke;
        Close();
        if (spoke) OnError?.Invoke(message);
        else OnUnreachable?.Invoke(message);
    }

    private static string BuildUrl()
    {
        var url = Settings.ServerUrl.Trim();
        var token = Settings.AuthToken.Trim();
        if (token.Length == 0) return url;
        var sep = url.Contains('?') ? '&' : '?';
        return $"{url}{sep}token={Uri.EscapeDataString(token)}";
    }

    private async Task RunAsync(ClientWebSocket socket, Uri url, CancellationToken ct)
    {
        try
        {
            await socket.ConnectAsync(url, ct).ConfigureAwait(false);
            await SendJsonAsync(new
            {
                type = "start",
                id = _dictationId,
                audio = new { rate = 16000, width = 2, channels = 1 },
            }, ct).ConfigureAwait(false);
            await ReceiveLoopAsync(socket, ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            // A deliberate Close() cancels the socket, which surfaces here — don't
            // report that as an error (ReportFailure's `closing` guard).
            ReportFailure(ex.Message);
        }
    }

    /// Fire-and-forget send of one PCM16 frame. Copies the slice because the
    /// caller's buffer is reused by NAudio on the next callback.
    public void SendAudio(byte[] data, int count)
    {
        var socket = _socket;
        if (socket is null || socket.State != WebSocketState.Open) return;
        var frame = new byte[count];
        Buffer.BlockCopy(data, 0, frame, 0, count);
        _ = SendBinaryAsync(frame);
    }

    private async Task SendBinaryAsync(byte[] frame)
    {
        var socket = _socket;
        var ct = _cts?.Token ?? CancellationToken.None;
        if (socket is null) return;
        await _sendGate.WaitAsync(ct).ConfigureAwait(false);
        string? failure = null;
        try
        {
            if (socket.State == WebSocketState.Open)
                await socket.SendAsync(frame, WebSocketMessageType.Binary, true, ct).ConfigureAwait(false);
        }
        catch (Exception ex) { failure = ex.Message; }
        finally { _sendGate.Release(); }
        // Reported outside the gate: ReportFailure ends up marshalling onto the
        // UI thread, and nothing that slow belongs inside the send lock.
        if (failure is not null) ReportFailure(failure);
    }

    /// Ask the server to finalize; it replies with a {final} message.
    public void Stop() => _ = SendJsonAsync(new { type = "stop", id = _dictationId },
        _cts?.Token ?? CancellationToken.None);

    public void Close()
    {
        _closing = true;
        _cts?.Cancel();
        try { _socket?.Abort(); } catch { /* ignore */ }
        _socket?.Dispose();
        _socket = null;
    }

    private async Task SendJsonAsync(object obj, CancellationToken ct)
    {
        var socket = _socket;
        if (socket is null) return;
        var bytes = Encoding.UTF8.GetBytes(JsonSerializer.Serialize(obj));
        await _sendGate.WaitAsync(ct).ConfigureAwait(false);
        string? failure = null;
        try
        {
            if (socket.State == WebSocketState.Open)
                await socket.SendAsync(bytes, WebSocketMessageType.Text, true, ct).ConfigureAwait(false);
        }
        catch (Exception ex) { failure = ex.Message; }
        finally { _sendGate.Release(); }
        if (failure is not null) ReportFailure(failure);
    }

    private async Task ReceiveLoopAsync(ClientWebSocket socket, CancellationToken ct)
    {
        var buffer = new byte[8192];
        var message = new MemoryStream();
        while (socket.State == WebSocketState.Open && !ct.IsCancellationRequested)
        {
            WebSocketReceiveResult result;
            try
            {
                // ArraySegment overload → Task<WebSocketReceiveResult>; the bare
                // Memory<byte> overload would instead return a ValueWebSocketReceiveResult.
                result = await socket.ReceiveAsync(new ArraySegment<byte>(buffer), ct).ConfigureAwait(false);
            }
            catch (Exception ex)
            {
                // Once the server has spoken, WebSocketException's own wording adds
                // nothing the user can act on; before it has, the cause matters.
                ReportFailure(_serverSpoke ? "Connection lost" : ex.Message);
                return;
            }

            if (result.MessageType == WebSocketMessageType.Close) return;

            message.Write(buffer, 0, result.Count);
            if (!result.EndOfMessage) continue;

            if (result.MessageType == WebSocketMessageType.Text)
                Handle(Encoding.UTF8.GetString(message.ToArray()));
            message.SetLength(0);
        }
    }

    private void Handle(string json)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (!root.TryGetProperty("type", out var typeEl)) return;
            // Anything at all from the server proves a blurtd is on the other
            // end. Noted before the id filter below, since even a message we go
            // on to drop is evidence the connection is alive.
            if (!_serverSpoke)
            {
                _serverSpoke = true;
                OnConnected?.Invoke();
            }
            // Drop messages from a dictation that isn't ours (a late final from
            // a previous session would otherwise get typed into the wrong
            // context). Connection-scoped messages like info carry no id.
            if (root.TryGetProperty("id", out var idEl)
                && idEl.ValueKind == JsonValueKind.String
                && idEl.GetString() is { Length: > 0 } id
                && id != _dictationId)
                return;
            var text = root.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "";
            switch (typeEl.GetString())
            {
                case "partial":
                    OnPartial?.Invoke(
                        root.TryGetProperty("committed", out var c) ? c.GetString() ?? "" : "",
                        root.TryGetProperty("live", out var l) ? l.GetString() ?? "" : "");
                    break;
                case "final": OnFinal?.Invoke(text); break;
                case "vad":
                    OnVad?.Invoke(root.TryGetProperty("speech", out var sp) && sp.GetBoolean());
                    break;
                case "info":
                    OnInfo?.Invoke(
                        root.TryGetProperty("state", out var st) ? st.GetString() ?? "" : "",
                        root.TryGetProperty("model", out var mo) ? mo.GetString() ?? "" : "");
                    break;
                case "status":
                    OnStatus?.Invoke(
                        root.TryGetProperty("state", out var s) ? s.GetString() ?? "" : "",
                        root.TryGetProperty("detail", out var d) ? d.GetString() : null);
                    break;
                // unknown types: ignored (forward compatibility)
            }
        }
        catch { /* malformed frame — ignore, matching the Swift best-effort parse */ }
    }
}
