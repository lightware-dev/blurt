import AVFoundation
import Foundation

/// WebSocket client to the Parakeet server (see docs/protocol.md). Sends
/// {start} with a fresh dictation id and the declared audio format, streams
/// PCM16, sends {stop}; surfaces info/vad/partial/final/status back on the
/// main queue. Messages tagged with a stale dictation id (a previous
/// dictation's late final, for example) are dropped.
final class DictationClient: NSObject, URLSessionDelegate, TranscriptionEngine {
    /// How long the server gets to say hello before we call it unreachable.
    ///
    /// The protocol has the server send `info` the instant the socket opens, so
    /// a live blurtd answers in milliseconds. A dead one, though, does not
    /// reliably *refuse*: a host asleep behind a VPN, a port forwarded to
    /// nothing, a daemon still coming up — all of those swallow the connection
    /// and then say nothing, and URLSession waits a full minute before
    /// admitting it. That minute used to pass with the mic live and the HUD
    /// reading "Listening…", so the dictation went into a socket that was never
    /// going to answer. Generous enough for a slow link, short enough that
    /// nobody gets through a sentence first.
    private static let handshakeTimeout: TimeInterval = 3

    /// How long past the server's own finalization budget to wait for a `final`
    /// before giving up on it. The server promises exactly one `final` per
    /// `stop`, degrading to the committed text rather than going quiet, so
    /// overshooting its budget means something wedged rather than something
    /// slow. The margin covers the network and a server busy enough to be late
    /// answering its own deadline.
    private static let finalizeGrace: TimeInterval = 15

    /// The server's finalization budget, from `info`. The default matches
    /// blurtd's own `STOP_TIMEOUT_S` default, for a server too old to advertise
    /// it — guessing low would cut off a legitimately slow decode.
    private var stopTimeout: TimeInterval = 60

    private var task: URLSessionWebSocketTask?
    private var closing = false
    private var dictationID = ""
    private lazy var session: URLSession =
        URLSession(configuration: .default, delegate: self, delegateQueue: nil)

    /// Whether anything has come back from the server on this connection. Until
    /// it has, we have no evidence of a blurtd on the other end — TCP and TLS
    /// both succeed against plenty of things that will never transcribe
    /// anything. Main queue only.
    private var serverSpoke = false
    /// Fires once the handshake budget runs out with the server still silent.
    private var watchdog: DispatchWorkItem?
    /// Fires once a `stop` has gone unanswered past the server's own budget.
    private var finalizeWatchdog: DispatchWorkItem?
    /// A dying connection can fail several ways at once (the receive loop and
    /// every queued send). The user needs to hear about it once.
    private var reportedFailure = false

    var onPartial: ((String, String) -> Void)?   // (committed, live) — live may still be revised
    var onFinal: ((String) -> Void)?
    var onVad: ((Bool) -> Void)?                 // server-side VAD: is it hearing speech?
    var onInfo: ((String, String) -> Void)?      // (state "ready|loading", model)
    var onStatus: ((String, String?) -> Void)?   // (state, detail)
    var onError: ((String) -> Void)?
    /// The server's first message — the only proof that dictation can actually
    /// happen. Fires once per connection, ahead of the message's own callback.
    var onConnected: (() -> Void)?
    /// The server never got as far as speaking: nothing is listening, the host
    /// is unreachable, or it accepted the connection and went quiet. Kept apart
    /// from `onError` because the cause is specific and the fix is actionable —
    /// start blurtd — where a mid-dictation drop is neither.
    var onUnreachable: ((String) -> Void)?
    /// `stop` was acknowledged by nothing: the socket is still up but the
    /// server's `final` never came, and waiting longer won't help.
    var onFinalizeTimeout: (() -> Void)?

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
        serverSpoke = false
        reportedFailure = false
        finalizeWatchdog?.cancel()   // defensive: a previous session's wait, if any
        finalizeWatchdog = nil
        dictationID = UUID().uuidString
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        sendJSON(["type": "start", "id": dictationID,
                  "audio": ["rate": 16000, "width": 2, "channels": 1]])
        receiveLoop()
        armWatchdog()
    }

    /// The wire format the server declares in `start` — the same one
    /// AudioCapture is told to produce, so this is a straight reinterpret of the
    /// buffer's bytes rather than a conversion.
    var inputFormat: AVAudioFormat { AudioCapture.serverFormat }

    func sendAudio(_ buffer: AVAudioPCMBuffer) {
        guard let ch = buffer.int16ChannelData, buffer.frameLength > 0 else { return }
        let data = Data(bytes: ch[0], count: Int(buffer.frameLength) * MemoryLayout<Int16>.size)
        task?.send(.data(data)) { [weak self] error in
            guard let error else { return }
            DispatchQueue.main.async { self?.reportFailure(error.localizedDescription) }
        }
    }

    /// Ask the server to finalize; it will reply with a {final} message.
    func stop() {
        sendJSON(["type": "stop", "id": dictationID])
        armFinalizeWatchdog()
    }

    func close() {
        closing = true
        watchdog?.cancel()
        watchdog = nil
        finalizeWatchdog?.cancel()
        finalizeWatchdog = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
    }

    private func sendJSON(_ obj: [String: Any]) {
        guard let d = try? JSONSerialization.data(withJSONObject: obj),
              let s = String(data: d, encoding: .utf8) else { return }
        task?.send(.string(s)) { [weak self] error in
            guard let error else { return }
            DispatchQueue.main.async { self?.reportFailure(error.localizedDescription) }
        }
    }

    /// Give up on a server that took the connection and then said nothing.
    /// Cancelled by its first message, and by `close()`. Main queue only.
    private func armWatchdog() {
        watchdog?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.serverSpoke else { return }
            self.reportFailure("No reply within \(Int(Self.handshakeTimeout)) seconds.")
        }
        watchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.handshakeTimeout, execute: work)
    }

    /// Stop waiting on a `final` that isn't coming. A wedged server (or one
    /// killed between the `stop` and the reply) leaves an otherwise healthy
    /// socket with nothing on it, which the receive loop has no way to notice —
    /// so without this the HUD sits on the last partial for good. Cancelled by
    /// the `final` itself, and by `close()`. Main queue only.
    private func armFinalizeWatchdog() {
        finalizeWatchdog?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.closing else { return }
            self.close()
            self.onFinalizeTimeout?()
        }
        finalizeWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + stopTimeout + Self.finalizeGrace,
                                      execute: work)
    }

    /// Report a dead connection once, tear the socket down, and route it by
    /// whether the server ever spoke: silence from the start is an unreachable
    /// server, a drop after it spoke is a lost connection. Main queue only.
    private func reportFailure(_ message: String) {
        guard !reportedFailure, !closing else { return }
        reportedFailure = true
        let spoke = serverSpoke
        close()
        if spoke { onError?(message) } else { onUnreachable?(message) }
    }

    private func receiveLoop() {
        task?.receive { [weak self] result in
            guard let self = self else { return }
            switch result {
            case .failure(let err):
                // A deliberate close() cancels the socket, which surfaces here as
                // a "socket is not connected" failure. Don't report that as an error.
                if self.closing { return }
                DispatchQueue.main.async { self.reportFailure(err.localizedDescription) }
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
            // Anything at all from the server proves a blurtd is on the other
            // end. Noted before the id filter below, since even a message we go
            // on to drop is evidence the connection is alive.
            if !self.serverSpoke {
                self.serverSpoke = true
                self.watchdog?.cancel()
                self.watchdog = nil
                self.onConnected?()
            }
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
            case "final":
                // Cancelled here rather than leaning on the close() that
                // follows in onFinal: if the final lands exactly on the
                // deadline both are already queued, and this one is first.
                self.finalizeWatchdog?.cancel()
                self.finalizeWatchdog = nil
                self.onFinal?(obj["text"] as? String ?? "")
            case "vad":    self.onVad?(obj["speech"] as? Bool ?? false)
            case "info":
                // Additive field: a server too old to send it leaves the
                // default standing (see stopTimeout).
                if let budget = obj["stop_timeout_s"] as? Double, budget > 0 {
                    self.stopTimeout = budget
                }
                self.onInfo?(obj["state"] as? String ?? "",
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
