import Foundation

/// WebSocket client to the Parakeet server. Sends {start}, streams PCM16, sends
/// {stop}; surfaces partial/final/status back on the main queue.
final class DictationClient: NSObject, URLSessionDelegate {
    private var task: URLSessionWebSocketTask?
    private var closing = false
    private lazy var session: URLSession =
        URLSession(configuration: .default, delegate: self, delegateQueue: nil)

    var onPartial: ((String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onStatus: ((String, String?) -> Void)?   // (state, detail)
    var onError: ((String) -> Void)?

    func connectAndStart() {
        guard var comps = URLComponents(string: Settings.serverURL) else {
            onError?("Bad server URL"); return
        }
        if !Settings.authToken.isEmpty {
            comps.queryItems = [URLQueryItem(name: "token", value: Settings.authToken)]
        }
        guard let url = comps.url else { onError?("Bad server URL"); return }
        closing = false
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        sendJSON(["type": "start"])
        receiveLoop()
    }

    func sendAudio(_ data: Data) {
        task?.send(.data(data)) { _ in }
    }

    /// Ask the server to finalize; it will reply with a {final} message.
    func stop() { sendJSON(["type": "stop"]) }

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
            switch type {
            case "partial": self.onPartial?(obj["text"] as? String ?? "")
            case "final":   self.onFinal?(obj["text"] as? String ?? "")
            case "status":  self.onStatus?(obj["state"] as? String ?? "", obj["detail"] as? String)
            default: break
            }
        }
    }

    // Trust the server's self-signed cert (LAN use). Remove for a public CA cert.
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if let trust = challenge.protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }
}
