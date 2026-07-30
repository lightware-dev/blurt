import Foundation

/// WebSocket client to the Parakeet server (see docs/protocol.md). Sends
/// {start} with a fresh dictation id and the declared audio format, streams
/// PCM16, sends {stop}; surfaces info/vad/partial/final/status back on the
/// main queue. Messages tagged with a stale dictation id (a previous
/// dictation's late final, for example) are dropped.
final class DictationClient: NSObject, URLSessionDelegate {
    private var task: URLSessionWebSocketTask?
    private var closing = false
    private var dictationID = ""
    private lazy var session: URLSession =
        URLSession(configuration: .default, delegate: self, delegateQueue: nil)

    var onPartial: ((String, String) -> Void)?   // (committed, live) — live may still be revised
    var onFinal: ((String) -> Void)?
    var onVad: ((Bool) -> Void)?                 // server-side VAD: is it hearing speech?
    var onInfo: ((String, String) -> Void)?      // (state "ready|loading", model)
    var onStatus: ((String, String?) -> Void)?   // (state, detail)
    var onError: ((String) -> Void)?

    /// Why the last handshake was refused, when it was refused over the server's
    /// certificate. A rejected TLS handshake surfaces to `onError` as an opaque
    /// "connection lost", so the app reads this to say what actually happened —
    /// and to offer to pin the new certificate without a second handshake.
    /// Main queue only.
    private(set) var certRejection: (decision: CertTrust.Decision, host: String, port: Int)?

    func connectAndStart() {
        // Cleared before the guards below, so a stale rejection can't make a
        // plain bad-URL error come back as a certificate dialog.
        certRejection = nil
        guard var comps = URLComponents(string: Settings.serverURL) else {
            onError?("Bad server URL"); return
        }
        if !Settings.authToken.isEmpty {
            comps.queryItems = [URLQueryItem(name: "token", value: Settings.authToken)]
        }
        guard let url = comps.url else { onError?("Bad server URL"); return }
        closing = false
        dictationID = UUID().uuidString
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        sendJSON(["type": "start", "id": dictationID,
                  "audio": ["rate": 16000, "width": 2, "channels": 1]])
        receiveLoop()
    }

    func sendAudio(_ data: Data) {
        task?.send(.data(data)) { _ in }
    }

    /// Ask the server to finalize; it will reply with a {final} message.
    func stop() { sendJSON(["type": "stop", "id": dictationID]) }

    func close() {
        closing = true
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func sendJSON(_ obj: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: obj),
              let s = String(data: d, encoding: .utf8) else { return }
        task?.send(.string(s)) { _ in }
    }

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err):
                // A deliberate close() cancels the socket, which surfaces here as
                // a "socket is not connected" failure. Don't report that as an error.
                if self.closing { return }
                DispatchQueue.main.async { self.onError?(err.localizedDescription) }
            case .success(let message):
                if case .string(let s) = message { self.handle(s) }
                self.receiveLoop()
            }
        }
    }

    private func handle(_ s: String) {
        guard let d = s.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let type = obj["type"] as? String else { return }
        DispatchQueue.main.async {
            // A partial can already be queued on main when close() is called
            // (e.g. Esc-cancel): delivering it would resurrect the HUD after
            // the "Cancelled" flash and leave it stuck on screen.
            guard !self.closing else { return }
            // Drop messages from a dictation that isn't ours (a late final from
            // a previous session would otherwise get typed into the wrong
            // context). Checked here rather than on the delegate queue so that
            // `dictationID` is only ever touched on main — this callback runs
            // on a URLSession queue, and a String assignment racing with a read
            // is not merely stale, it's undefined. Connection-scoped messages
            // like info carry no id and pass through.
            if let id = obj["id"] as? String, !id.isEmpty, id != self.dictationID { return }
            switch type {
            case "partial":
                self.onPartial?(obj["committed"] as? String ?? "",
                                obj["live"] as? String ?? "")
            case "final":  self.onFinal?(obj["text"] as? String ?? "")
            case "vad":    self.onVad?(obj["speech"] as? Bool ?? false)
            case "info":   self.onInfo?(obj["state"] as? String ?? "",
                                        obj["model"] as? String ?? "")
            case "status": self.onStatus?(obj["state"] as? String ?? "", obj["detail"] as? String)
            default: break   // unknown types: ignored (forward compatibility)
            }
        }
    }

    /// Validate the server's certificate — silently. A certificate that is
    /// system-valid or matches the pin for this host is accepted; anything else
    /// drops the connection and records why in `certRejection`.
    ///
    /// Deliberately never prompts: by the time this runs the HUD is up and the
    /// mic is live, so a trust dialog here would eat the user's first sentence.
    /// CertTrust.preflight settles trust before recording ever starts.
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        let host = challenge.protectionSpace.host
        let port = challenge.protectionSpace.port
        let decision = CertTrust.evaluate(trust: trust, host: host, port: port)
        if case .trusted = decision {
            DispatchQueue.main.async { CertTrust.markVerified(CertTrust.pinKey(host: host, port: port)) }
            completionHandler(.useCredential, URLCredential(trust: trust))
            return
        }
        // Enqueued before the handshake is failed, so it lands on main ahead of
        // the onError that reads it.
        DispatchQueue.main.async {
            self.certRejection = (decision, host, port)
            // So the next dictation settles this up front rather than bringing
            // the mic up and failing the same way again.
            CertTrust.markRejected(CertTrust.pinKey(host: host, port: port))
        }
        completionHandler(.cancelAuthenticationChallenge, nil)
    }
}
