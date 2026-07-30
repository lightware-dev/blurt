using System.Net.Http;
using System.Net.Security;
using System.Security.Cryptography;
using System.Security.Cryptography.X509Certificates;

namespace Blurt;

/// What the certificate the server just presented means for the connection.
internal enum TrustKind
{
    Trusted,       // system-valid, or a pin match
    FirstUse,      // self-signed, nothing pinned yet
    Changed,       // self-signed, pin mismatch
    Unverifiable,  // no readable certificate
}

/// <param name="Fingerprint">Uppercase hex SHA-256 over the certificate's DER bytes.</param>
/// <param name="Pinned">The fingerprint previously trusted for this host, if any.</param>
internal sealed record TrustDecision(
    TrustKind Kind, string Host, int Port, string Fingerprint, string? Pinned)
{
    public string Key => CertTrust.PinKey(Host, Port);
}

/// Trust On First Use pinning for the server's TLS certificate — the Windows
/// twin of the Mac client's CertTrust.swift, with the same four-way decision:
///
/// 1. a certificate that validates against the system trust store connects
///    silently and is never pinned — a real CA certificate just works;
/// 2. a self-signed certificate with no pin for this host is confirmed once by
///    the user, then pinned;
/// 3. a self-signed certificate matching the stored pin connects silently;
/// 4. a certificate that *differs* from the stored pin raises a distinctly
///    scarier warning and is only re-pinned on explicit confirmation, so
///    re-running `gen_certs.sh` stays recoverable without silently swallowing
///    an imposter.
///
/// Pins are keyed by lowercased `host:port`, so changing the server URL doesn't
/// drop trust for the old host and localhost is tracked separately from a LAN
/// address.
///
/// The dictation socket's own handshake never prompts (see
/// DictationClient.ConnectAndStart) — trust is settled ahead of time by
/// <see cref="PreflightAsync"/>, called at launch, whenever the server URL
/// changes, and once more before recording starts if this host is still
/// unsettled. A trust dialog on the live handshake would land on top of a HUD
/// already saying "Listening…" and eat whatever the user said while it was on
/// screen.
internal static class CertTrust
{
    public enum Outcome
    {
        Ok,           // trusted — go ahead and connect
        Refused,      // the user declined; don't connect
        Unreachable,  // no certificate seen at all (server down, bad URL)
    }

    /// Shows the trust dialog and returns true to trust. Set by BlurtApp, which
    /// marshals it onto the UI thread — unlike the Mac twin, where the alert can
    /// be built in place, WPF windows must be created on the dispatcher thread.
    public static Func<TrustDecision, bool>? Confirm;

    /// Hosts settled during this run. A CA-backed certificate is deliberately
    /// never pinned, so without this cache <see cref="NeedsCheck"/> would probe
    /// on every single dictation. Guarded by its own lock — reached from the UI
    /// thread, the pre-flight continuation, and the TLS callback's thread.
    private static readonly HashSet<string> Verified = new();
    /// Hosts whose last handshake was refused over their certificate. Without
    /// this, a user who declines a changed certificate keeps the stale pin — and
    /// <see cref="NeedsCheck"/> would wave every later dictation straight past
    /// the pre-flight into the mic-first, dialog-second path this whole file
    /// exists to avoid.
    private static readonly HashSet<string> Rejected = new();

    public static string PinKey(string host, int port) => $"{host.ToLowerInvariant()}:{port}";

    /// Records a host as settled for the rest of this run.
    public static void MarkVerified(string key)
    {
        lock (Verified) { Verified.Add(key); Rejected.Remove(key); }
    }

    /// Records that this host's certificate was refused, so the next dictation
    /// settles it up front instead of failing again with the HUD already up.
    public static void MarkRejected(string key)
    {
        lock (Verified) { Rejected.Add(key); Verified.Remove(key); }
    }

    // ── evaluating ───────────────────────────────────────────

    /// Judge a server's certificate. Pure and silent — safe to call from a TLS
    /// validation callback, and it never shows UI.
    public static TrustDecision Evaluate(X509Certificate? cert, string host, int port, SslPolicyErrors errors)
    {
        // A certificate that chains to a system-trusted root needs no pin: the
        // OS already authenticated it, and pinning it would only break the day
        // it's legitimately renewed.
        if (errors == SslPolicyErrors.None)
            return new TrustDecision(TrustKind.Trusted, host, port, "", null);
        if (cert is null) return new TrustDecision(TrustKind.Unverifiable, host, port, "", null);

        // The same digest `openssl x509 -fingerprint -sha256` prints, and the
        // same one the Mac client computes over the leaf's DER bytes.
        var fingerprint = cert.GetCertHashString(HashAlgorithmName.SHA256);
        var pinned = Settings.PinnedFingerprint(PinKey(host, port));
        if (pinned is null)
            return new TrustDecision(TrustKind.FirstUse, host, port, fingerprint, null);
        if (string.Equals(pinned, fingerprint, StringComparison.OrdinalIgnoreCase))
            return new TrustDecision(TrustKind.Trusted, host, port, fingerprint, pinned);
        return new TrustDecision(TrustKind.Changed, host, port, fingerprint, pinned);
    }

    /// Ask the user about a certificate that isn't trusted yet and, if they
    /// accept, pin it. Returns whether the connection may proceed.
    public static bool PromptAndPin(TrustDecision decision)
    {
        if (decision.Kind == TrustKind.Trusted) return true;
        // Another pre-flight may have pinned this very certificate while the
        // dialog was up; don't ask twice for the same answer.
        if (decision.Fingerprint.Length > 0
            && string.Equals(Settings.PinnedFingerprint(decision.Key), decision.Fingerprint,
                             StringComparison.OrdinalIgnoreCase))
        {
            MarkVerified(decision.Key);
            return true;
        }
        if (Confirm is not { } confirm || !confirm(decision)) return false;
        // Belt and braces: never pin an empty fingerprint.
        if (decision.Kind == TrustKind.Unverifiable) return false;
        Settings.SetPinnedFingerprint(decision.Fingerprint, decision.Key);
        MarkVerified(decision.Key);
        return true;
    }

    // ── pre-flight ───────────────────────────────────────────

    /// Whether trust for this URL still has to be settled before recording can
    /// start. False for `ws://`, for a pinned host, and for a host already
    /// settled this run — but true again once a handshake has been refused.
    public static bool NeedsCheck(string url)
    {
        if (ProbeUri(url) is not { } probe) return false;
        var key = PinKey(probe.Host, probe.Port);
        lock (Verified)
        {
            if (Rejected.Contains(key)) return true;
            if (Verified.Contains(key)) return false;
        }
        return Settings.PinnedFingerprint(key) is null;
    }

    /// The probe currently running. A launch-time check and a hotkey press can
    /// easily overlap; without this the second one would stack a duplicate
    /// dialog for the same certificate on top of the first.
    private static readonly object ProbeGate = new();
    private static Uri? _inFlightUri;
    private static Task<Outcome>? _inFlight;

    /// Open a throwaway TLS connection to the server purely to see its
    /// certificate, prompting if it isn't trusted yet. A `ws://` URL has nothing
    /// to check and reports <see cref="Outcome.Ok"/>.
    public static Task<Outcome> PreflightAsync(string url)
    {
        if (ProbeUri(url) is not { } probe) return Task.FromResult(Outcome.Ok);
        lock (ProbeGate)
        {
            if (_inFlight is { IsCompleted: false } running)
            {
                // Same server: ride along on the answer the user is already
                // giving. A different one means its dialog is up for somewhere
                // else, so hold off rather than starting a second conversation
                // behind it.
                return _inFlightUri == probe ? running : Task.FromResult(Outcome.Refused);
            }
            _inFlightUri = probe;
            // Starts running here, but only as far as the first await — no UI
            // hop happens under the lock.
            return _inFlight = ProbeAsync(probe);
        }
    }

    private static async Task<Outcome> ProbeAsync(Uri probe)
    {
        TrustDecision? seen = null;
        using var handler = new HttpClientHandler
        {
            // A redirect to another origin would hand this callback a stranger's
            // certificate, which we'd then judge — and offer to pin — under the
            // Blurt server's host:port. Nothing here needs the response anyway.
            AllowAutoRedirect = false,
            ServerCertificateCustomValidationCallback = (_, cert, _, errors) =>
            {
                seen = Evaluate(cert, probe.Host, probe.Port, errors);
                return seen.Kind == TrustKind.Trusted;
            },
        };
        // Short: an unreachable server makes the user wait this long before the
        // HUD appears when the pre-flight is the one gating recording.
        using var http = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(2) };
        try
        {
            // The response is irrelevant — an auth-gated 401 is as good as a 200.
            // Only the handshake matters, and rejecting the certificate above
            // throws right here, by which point `seen` already holds the verdict.
            using var _ = await http.GetAsync(probe, HttpCompletionOption.ResponseHeadersRead)
                .ConfigureAwait(false);
        }
        catch { /* see above */ }

        if (seen is null) return Outcome.Unreachable;
        if (seen.Kind == TrustKind.Trusted) { MarkVerified(seen.Key); return Outcome.Ok; }
        // Prompting out here rather than inside the callback keeps the UI hop off
        // the TLS handshake's thread.
        return PromptAndPin(seen) ? Outcome.Ok : Outcome.Refused;
    }

    /// The server serves its browser mic-test page over HTTPS on the same port
    /// it serves `wss://` on, so a plain GET at the origin root reaches the same
    /// listener with the same certificate. The token is dropped along with the
    /// path — the handshake is the whole point.
    private static Uri? ProbeUri(string url)
    {
        if (!Uri.TryCreate(url.Trim(), UriKind.Absolute, out var parsed)) return null;
        if (!string.Equals(parsed.Scheme, "wss", StringComparison.OrdinalIgnoreCase)) return null;
        return new UriBuilder("https", parsed.Host, Port(parsed)).Uri;
    }

    /// .NET resolves `wss://` with no explicit port to 443, matching what the
    /// Mac client sees in its protection space — so the two clients derive the
    /// same pin key. The guard is for an unparsed port only.
    public static int Port(Uri url) => url.Port < 0 ? 443 : url.Port;
}
