import AppKit
import CryptoKit
import Foundation
import Security

/// Trust On First Use pinning for the server's TLS certificate.
///
/// Blurt talks to a box on your own LAN whose certificate is almost always
/// self-signed, so there is no authority to check it against. The clients used
/// to answer that by trusting *any* certificate the server offered, which left
/// the connection unauthenticated against a man-in-the-middle. Instead:
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
/// The dictation socket's own handshake never prompts (see `evaluate`) — trust
/// is settled ahead of time by `preflight`, called at launch, whenever the
/// server URL changes, and once more before recording starts if this host is
/// still unsettled. A trust dialog on the live handshake would land on top of a
/// HUD already saying "Listening…" and eat whatever the user said while it was
/// on screen.
enum CertTrust {
    /// What the certificate the server just presented means for the connection.
    enum Decision {
        case trusted                            // system-valid, or a pin match
        case firstUse(String)                   // self-signed, nothing pinned yet
        case changed(old: String, new: String)  // self-signed, pin mismatch
        case unverifiable                       // no readable leaf certificate
    }

    enum Outcome {
        case ok           // trusted — go ahead and connect
        case refused      // the user declined; don't connect
        case unreachable  // no certificate seen at all (server down, bad URL)
    }

    // Everything below is main-thread only.

    /// Hosts settled during this run. A CA-backed certificate is deliberately
    /// never pinned, so without this cache `needsCheck` would probe on every
    /// single dictation.
    private static var verified = Set<String>()
    /// Hosts whose last handshake was refused over their certificate. Without
    /// this, a user who declines a changed certificate keeps the stale pin — and
    /// `needsCheck` would wave every later dictation straight past the pre-flight
    /// into the mic-first, dialog-second path this whole file exists to avoid.
    private static var rejected = Set<String>()

    static func pinKey(host: String, port: Int) -> String {
        "\(host.lowercased()):\(port)"
    }

    /// Records a host as settled for the rest of this run.
    static func markVerified(_ key: String) {
        verified.insert(key)
        rejected.remove(key)
    }

    /// Records that this host's certificate was refused, so the next dictation
    /// settles it up front instead of failing again with the HUD already up.
    static func markRejected(_ key: String) {
        rejected.insert(key)
        verified.remove(key)
    }

    // MARK: evaluating

    /// Judge a server's certificate. Pure and silent — safe to call from a
    /// URLSession delegate queue, and it never shows UI.
    static func evaluate(trust: SecTrust, host: String, port: Int) -> Decision {
        // A certificate that chains to a system-trusted root needs no pin: the
        // OS already authenticated it, and pinning it would only break the day
        // it's legitimately renewed.
        if SecTrustEvaluateWithError(trust, nil) { return .trusted }
        guard let fingerprint = leafFingerprint(trust) else { return .unverifiable }
        guard let pinned = Settings.pinnedFingerprint(host: pinKey(host: host, port: port)) else {
            return .firstUse(fingerprint)
        }
        if pinned.caseInsensitiveCompare(fingerprint) == .orderedSame { return .trusted }
        return .changed(old: pinned, new: fingerprint)
    }

    /// Uppercase hex SHA-256 over the leaf certificate's DER bytes — the same
    /// digest `openssl x509 -fingerprint -sha256` prints, and the same one the
    /// Windows client computes with `GetCertHashString`.
    private static func leafFingerprint(_ trust: SecTrust) -> String? {
        guard let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
              let leaf = chain.first else { return nil }
        let der = SecCertificateCopyData(leaf) as Data
        return SHA256.hash(data: der).map { String(format: "%02X", $0) }.joined()
    }

    // MARK: prompting

    /// Ask the user about a certificate that isn't trusted yet and, if they
    /// accept, pin it. Returns whether the connection may proceed. Main thread
    /// only — it runs a modal alert.
    ///
    /// (The Windows twin routes this through a closure so it can marshal onto
    /// the UI thread; on macOS `NSAlert` is fine to build anywhere on main, so
    /// the dialog lives here next to the decision it explains.)
    @discardableResult
    static func promptAndPin(_ decision: Decision, host: String, port: Int) -> Bool {
        let key = pinKey(host: host, port: port)

        // Another pre-flight may have pinned this very certificate while this
        // one was queued behind it; don't ask twice for the same answer.
        if let offered = offeredFingerprint(decision),
           Settings.pinnedFingerprint(host: key)?.caseInsensitiveCompare(offered) == .orderedSame {
            markVerified(key)
            return true
        }

        let alert = NSAlert()

        switch decision {
        case .trusted:
            return true

        case .unverifiable:
            alert.alertStyle = .critical
            alert.messageText = "Couldn't read the server's certificate"
            alert.informativeText = """
                Blurt got no usable certificate from \(key), so the connection \
                can't be authenticated.
                """
            alert.addButton(withTitle: "OK")
            _ = run(alert)
            return false

        case .firstUse(let fingerprint):
            alert.messageText = "Trust this server's certificate?"
            alert.informativeText = """
                Blurt is connecting to \(key) for the first time. Its certificate is \
                self-signed, so there's no authority to check it against.

                SHA-256 fingerprint:
                \(pretty(fingerprint))

                Trust it only if this is your own Blurt server.
                """
            alert.addButton(withTitle: "Trust")
            alert.addButton(withTitle: "Cancel")
            guard run(alert) == .alertFirstButtonReturn else { return false }
            pin(fingerprint, key: key)
            return true

        case .changed(let old, let new):
            // Cancel is deliberately the default button here: the safe answer to
            // a certificate that moved under us is "no".
            alert.alertStyle = .critical
            alert.messageText = "The certificate for \(host) has changed"
            alert.informativeText = """
                Only continue if you regenerated the certificate on your Blurt \
                server yourself. Otherwise something else could be impersonating it.

                New fingerprint:
                \(pretty(new))

                Previously trusted:
                \(pretty(old))
                """
            alert.addButton(withTitle: "Cancel")
            alert.addButton(withTitle: "Trust New Certificate")
            guard run(alert) == .alertSecondButtonReturn else { return false }
            pin(new, key: key)
            return true
        }
    }

    /// The fingerprint a decision is asking the user to vouch for, if any.
    private static func offeredFingerprint(_ decision: Decision) -> String? {
        switch decision {
        case .firstUse(let fingerprint):  return fingerprint
        case .changed(_, let new):        return new
        case .trusted, .unverifiable:     return nil
        }
    }

    private static func pin(_ fingerprint: String, key: String) {
        Settings.setPinnedFingerprint(fingerprint, host: key)
        markVerified(key)
    }

    private static func run(_ alert: NSAlert) -> NSApplication.ModalResponse {
        NSApp.activate(ignoringOtherApps: true)
        return alert.runModal()
    }

    /// `AB:CD:…` in two lines of 16 bytes, so a 32-byte digest stays readable
    /// and comparable against what the server prints.
    private static func pretty(_ hex: String) -> String {
        let bytes = stride(from: 0, to: hex.count, by: 2).map { i -> String in
            let start = hex.index(hex.startIndex, offsetBy: i)
            let end = hex.index(start, offsetBy: min(2, hex.count - i))
            return String(hex[start..<end])
        }
        return bytes.chunked(16).map { $0.joined(separator: ":") }.joined(separator: "\n")
    }

    // MARK: pre-flight

    /// Whether trust for this URL still has to be settled before recording can
    /// start. False for `ws://`, for a pinned host, and for a host already
    /// settled this run — but true again once a handshake has been refused.
    static func needsCheck(_ urlString: String) -> Bool {
        guard let url = probeURL(urlString), let host = url.host else { return false }
        let key = pinKey(host: host, port: url.port ?? 443)
        if rejected.contains(key) { return true }
        return Settings.pinnedFingerprint(host: key) == nil && !verified.contains(key)
    }

    /// The probe currently running, and who's waiting on it. A launch-time check
    /// and a hotkey press can easily overlap; without this the second one would
    /// stack a duplicate dialog for the same certificate on top of the first.
    /// (The probe itself needs no strong reference here — URLSession retains it
    /// as its delegate until `invalidateAndCancel`.)
    private static var inFlight: URL?
    private static var waiting: [(Outcome) -> Void] = []

    /// Open a throwaway TLS connection to the server purely to see its
    /// certificate, prompting if it isn't trusted yet. `completion` runs on the
    /// main queue, exactly once. A `ws://` URL has nothing to check and reports
    /// `.ok`. Main thread only.
    static func preflight(_ urlString: String, completion: @escaping (Outcome) -> Void = { _ in }) {
        guard let url = probeURL(urlString) else { completion(.ok); return }

        if let running = inFlight {
            // Same server: ride along on the answer the user is already giving.
            // A different one means its dialog is up for somewhere else, so hold
            // off rather than starting a second conversation behind it.
            if running == url { waiting.append(completion) } else { completion(.refused) }
            return
        }

        inFlight = url
        waiting = [completion]
        Probe().run(url) { outcome in
            inFlight = nil
            let waiters = waiting
            waiting = []
            waiters.forEach { $0(outcome) }
        }
    }

    /// Whether `preflight` will actually open a connection for this URL. False
    /// for `ws://`, which has no certificate to inspect and so reports `.ok`
    /// without touching the network — an answer about trust, not about whether
    /// anything is listening. Callers reading reachability out of an `Outcome`
    /// have to check this first.
    static func probesReachability(_ urlString: String) -> Bool {
        probeURL(urlString) != nil
    }

    /// The server serves its browser mic-test page over HTTPS on the same port
    /// it serves `wss://` on, so a plain GET at the origin root reaches the same
    /// listener with the same certificate. The response is irrelevant — the
    /// handshake is the whole point — so the token is dropped along with the path.
    private static func probeURL(_ urlString: String) -> URL? {
        guard var comps = URLComponents(string: urlString),
              comps.scheme?.lowercased() == "wss" else { return nil }
        comps.scheme = "https"
        comps.path = "/"
        comps.query = nil
        comps.fragment = nil
        return comps.url
    }

    /// One throwaway HTTPS request whose only job is to trigger a TLS handshake
    /// we can judge. URLSession keeps it alive via its delegate reference until
    /// `invalidateAndCancel`.
    private final class Probe: NSObject, URLSessionDelegate, URLSessionTaskDelegate {
        private var session: URLSession?
        private var finish: ((Outcome) -> Void)?
        /// Set from the handshake, consumed on main once the request is over.
        private var outcome: Outcome = .unreachable
        private var pending: (decision: Decision, host: String, port: Int)?

        func run(_ url: URL, completion: @escaping (Outcome) -> Void) {
            finish = completion
            // Short: an unreachable server makes the user wait this long before
            // the HUD appears when the pre-flight is the one gating recording.
            // Safe to keep tight because nothing here waits on a human — the
            // dialog runs after the request is done, not during it.
            let config = URLSessionConfiguration.ephemeral
            config.timeoutIntervalForRequest = 2
            config.timeoutIntervalForResource = 4
            let session = URLSession(configuration: config, delegate: self, delegateQueue: nil)
            self.session = session
            var request = URLRequest(url: url)
            request.httpMethod = "HEAD"
            session.dataTask(with: request) { [weak self] _, _, _ in self?.complete() }.resume()
        }

        private func complete() {
            DispatchQueue.main.async {
                guard let finish = self.finish else { return }
                self.finish = nil
                self.session?.invalidateAndCancel()
                self.session = nil
                // Prompt here rather than inside the challenge: an NSAlert held
                // the handshake open for as long as the user took to read a
                // fingerprint, which blew the request timeout and threw their
                // answer away — while `runModal` drained the main queue and let
                // the HUD come up behind the dialog.
                if let pending = self.pending {
                    self.pending = nil
                    self.outcome = CertTrust.promptAndPin(
                        pending.decision, host: pending.host, port: pending.port) ? .ok : .refused
                }
                finish(self.outcome)
            }
        }

        /// Refuse redirects. Following one to another origin would hand the
        /// challenge below a stranger's certificate, which we'd then offer to
        /// pin. Nothing here needs the response anyway.
        func urlSession(_ session: URLSession, task: URLSessionTask,
                        willPerformHTTPRedirection response: HTTPURLResponse,
                        newRequest request: URLRequest,
                        completionHandler: @escaping (URLRequest?) -> Void) {
            completionHandler(nil)
        }

        func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            guard let trust = challenge.protectionSpace.serverTrust else {
                completionHandler(.performDefaultHandling, nil)
                return
            }
            let host = challenge.protectionSpace.host
            let port = challenge.protectionSpace.port
            let decision = CertTrust.evaluate(trust: trust, host: host, port: port)

            // Both branches enqueue on main *before* answering the challenge, so
            // they land ahead of the task-completion block that reads them: the
            // delegate queue is serial, and the main queue is FIFO.
            if case .trusted = decision {
                DispatchQueue.main.async {
                    self.outcome = .ok
                    CertTrust.markVerified(CertTrust.pinKey(host: host, port: port))
                }
                completionHandler(.useCredential, URLCredential(trust: trust))
                return
            }

            // Drop the connection now and decide afterwards. Nothing needs this
            // one even if the user trusts the certificate — the pin gets stored
            // and the dictation socket handshakes for real.
            DispatchQueue.main.async { self.pending = (decision, host, port) }
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }
}

private extension Array {
    func chunked(_ size: Int) -> [[Element]] {
        stride(from: 0, to: count, by: size).map { Array(self[$0..<Swift.min($0 + size, count)]) }
    }
}
