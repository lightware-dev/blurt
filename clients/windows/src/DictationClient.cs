using System.Net.WebSockets;
using System.Text;
using System.Text.Json;

namespace Blurt;

/// WebSocket client to the Parakeet server, the twin of the Mac client's
/// DictationClient.swift. Sends {start}, streams PCM16 binary frames, sends
/// {stop}; surfaces partial/final/status/error back to the caller. Callbacks fire
/// on a background thread — the app marshals them onto the UI thread.
internal sealed class DictationClient
{
    public Action<string>? OnPartial;
    public Action<string>? OnFinal;
    public Action<string>? OnStatus;
    public Action<string>? OnError;

    private ClientWebSocket? _socket;
    private CancellationTokenSource? _cts;
    private volatile bool _closing;

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
        _cts = new CancellationTokenSource();
        var socket = new ClientWebSocket();
        // Trust the server's self-signed cert (LAN use), matching the Mac client's
        // URLSession trust-all delegate. Remove for a public CA cert.
        socket.Options.RemoteCertificateValidationCallback = (_, _, _, _) => true;
        _socket = socket;

        _ = RunAsync(socket, url, _cts.Token);
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
            await SendJsonAsync(new { type = "start" }, ct).ConfigureAwait(false);
            await ReceiveLoopAsync(socket, ct).ConfigureAwait(false);
        }
        catch (Exception ex)
        {
            // A deliberate Close() cancels the socket, which surfaces here — don't
            // report that as an error (mirrors the Swift `closing` guard).
            if (!_closing) OnError?.Invoke(ex.Message);
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
        try
        {
            if (socket.State == WebSocketState.Open)
                await socket.SendAsync(frame, WebSocketMessageType.Binary, true, ct).ConfigureAwait(false);
        }
        catch { /* transient send failure; the receive loop will surface a real error */ }
        finally { _sendGate.Release(); }
    }

    /// Ask the server to finalize; it replies with a {final} message.
    public void Stop() => _ = SendJsonAsync(new { type = "stop" }, _cts?.Token ?? CancellationToken.None);

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
        try
        {
            if (socket.State == WebSocketState.Open)
                await socket.SendAsync(bytes, WebSocketMessageType.Text, true, ct).ConfigureAwait(false);
        }
        catch { /* ignore */ }
        finally { _sendGate.Release(); }
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
            catch
            {
                if (!_closing) OnError?.Invoke("Connection lost");
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
            var text = root.TryGetProperty("text", out var t) ? t.GetString() ?? "" : "";
            switch (typeEl.GetString())
            {
                case "partial": OnPartial?.Invoke(text); break;
                case "final": OnFinal?.Invoke(text); break;
                case "status":
                    OnStatus?.Invoke(root.TryGetProperty("state", out var s) ? s.GetString() ?? "" : "");
                    break;
            }
        }
        catch { /* malformed frame — ignore, matching the Swift best-effort parse */ }
    }
}
