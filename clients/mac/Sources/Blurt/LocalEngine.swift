import AVFoundation
import Foundation
import Speech
import os

/// Transcribes on this Mac, using the Speech framework's on-device analyzer
/// (macOS 26, Apple silicon — see LocalTranscription.isSupported).
///
/// Apple's result model is the one Blurt already had: a *volatile* result is the
/// sentence being revised as you speak, and a *finalized* one is settled text
/// that will not change. Those are exactly the `live` and `committed` halves of
/// the HUD's partial, so the mapping is direct and the HUD needs no changes.
///
/// A `SpeechDetector` rides alongside the transcriber purely so the HUD's
/// "Hearing you…" state means the same thing it does in server mode.
///
/// `@unchecked Sendable` because the concurrency discipline here is one the
/// compiler can't see: every stored property is main-queue-only, except the
/// continuation, which is behind a lock precisely because the audio thread
/// yields into it while main can be tearing it down.
@available(macOS 26, *)
final class LocalEngine: TranscriptionEngine, @unchecked Sendable {

    /// How long past the end of speech to wait for the analyzer to finish. It is
    /// working from audio it already has, on-device, so this is generous — it
    /// exists to stop a wedged analyzer leaving the HUD on the last partial
    /// forever, not to bound normal work.
    private static let finalizeTimeout: TimeInterval = 20

    /// What to feed the analyzer before it has told us what it wants. Float32
    /// mono 16 kHz is what `bestAvailableAudioFormat` returns in practice; if a
    /// given OS build disagrees, `convert(_:)` fixes it up rather than failing.
    private static let fallbackFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32,
                                                      sampleRate: 16000, channels: 1,
                                                      interleaved: false)!

    var onPartial: ((String, String) -> Void)?
    var onFinal: ((String) -> Void)?
    var onVad: ((Bool) -> Void)?
    var onInfo: ((String, String) -> Void)?
    var onStatus: ((String, String?) -> Void)?
    var onError: ((String) -> Void)?
    var onConnected: (() -> Void)?
    /// Never fires: nothing to be unreachable about on-device. Part of the
    /// protocol so AppDelegate can wire one set of callbacks for both engines.
    var onUnreachable: ((String) -> Void)?
    var onFinalizeTimeout: (() -> Void)?

    /// The analyzer's preferred format once known, resolved during `prewarm()`.
    /// Locked because AppDelegate reads it on main to configure AudioCapture
    /// while `convert(_:)` reads it on the audio thread, and negotiation can
    /// land between the two.
    private let formatBox = OSAllocatedUnfairLock<AVAudioFormat?>(uncheckedState: nil)
    var inputFormat: AVAudioFormat { formatBox.withLock { $0 } ?? Self.fallbackFormat }

    private var analyzer: SpeechAnalyzer?
    private var resultsTask: Task<Void, Never>?
    private var vadTask: Task<Void, Never>?
    private var finalizeWatchdog: DispatchWorkItem?

    /// The audio thread yields into this while main can be tearing it down, so
    /// both sides go through the lock. Everything else here is main-queue only.
    /// `OSAllocatedUnfairLock` rather than `NSLock` because `stop()` and
    /// `close()` touch it from async contexts, where NSLock is unavailable.
    private let continuationBox =
        OSAllocatedUnfairLock<AsyncStream<AnalyzerInput>.Continuation?>(initialState: nil)

    /// Fixes up a format mismatch if the analyzer wants something other than
    /// what AudioCapture was configured with — only possible when a dictation
    /// starts before `prewarm()` has finished negotiating. Audio thread only.
    private var fixup: AVAudioConverter?

    private var committed = ""
    private var live = ""
    /// Set by `close()`. Late results from a torn-down analyzer are dropped
    /// rather than resurrecting the HUD after a cancel.
    private var closed = false
    private var prewarmed = false
    private var reportedFailure = false

    // MARK: lifecycle

    /// Resolve the audio format and force the model resident, so the first
    /// dictation isn't what pays to load it. Deliberately does *not* download a
    /// missing language asset — that can be hundreds of megabytes, and doing it
    /// unasked at launch is not ours to decide. The first dictation downloads it
    /// with the HUD saying so.
    func prewarm() {
        guard !prewarmed else { return }
        prewarmed = true
        Task { [weak self] in
            guard let self else { return }
            let modules = await Self.makeModules()
            guard let modules else { return }
            let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: modules.all)
            formatBox.withLock { $0 = format }
            guard await AssetInventory.status(forModules: modules.all) == .installed else { return }
            let analyzer = SpeechAnalyzer(modules: modules.all,
                                          options: .init(priority: .utility,
                                                         modelRetention: .processLifetime))
            try? await analyzer.prepareToAnalyze(in: format)
        }
    }

    func connectAndStart() {
        closed = false
        reportedFailure = false
        committed = ""
        live = ""
        Task { [weak self] in await self?.begin() }
    }

    func sendAudio(_ buffer: AVAudioPCMBuffer) {
        guard let buffer = convert(buffer) else { return }
        continuationBox.withLock { $0 }?.yield(AnalyzerInput(buffer: buffer))
    }

    /// Close the input and ask the analyzer to finish what it has. The results
    /// stream ends once everything is finalized, which is when the transcript is
    /// complete and can be delivered.
    func stop() {
        armFinalizeWatchdog()
        Task { [weak self] in
            guard let self else { return }
            self.finishInput()
            do {
                try await self.analyzer?.finalizeAndFinishThroughEndOfInput()
            } catch {
                DispatchQueue.main.async { self.reportFailure(error.localizedDescription) }
                return
            }
            // The results task drains the stream to completion, so `committed`
            // isn't whole until it has returned.
            await self.resultsTask?.value
            DispatchQueue.main.async {
                guard !self.closed else { return }
                self.finalizeWatchdog?.cancel()
                self.finalizeWatchdog = nil
                self.onFinal?(Self.join(self.committed, self.live))
            }
        }
    }

    func close() {
        closed = true
        finalizeWatchdog?.cancel()
        finalizeWatchdog = nil
        finishInput()
        resultsTask?.cancel()
        vadTask?.cancel()
        resultsTask = nil
        vadTask = nil
        let analyzer = analyzer
        self.analyzer = nil
        Task { await analyzer?.cancelAndFinishNow() }
    }

    private func finishInput() {
        continuationBox.withLock { taken -> AsyncStream<AnalyzerInput>.Continuation? in
            defer { taken = nil }
            return taken
        }?.finish()
    }

    // MARK: setup

    private struct Modules {
        let transcriber: SpeechTranscriber
        let detector: SpeechDetector
        let locale: Locale
        var all: [any SpeechModule] { [transcriber, detector] }
    }

    private static func makeModules() async -> Modules? {
        guard let locale = await resolveLocale() else { return nil }
        return Modules(
            transcriber: SpeechTranscriber(locale: locale,
                                           transcriptionOptions: [],
                                           reportingOptions: [.volatileResults],
                                           attributeOptions: []),
            detector: SpeechDetector(detectionOptions: .init(sensitivityLevel: .medium),
                                     reportResults: true),
            locale: locale)
    }

    /// The transcription language: whatever the user's Mac is set to, if the
    /// framework has a model for it, else English, else whatever it does have.
    private static func resolveLocale() async -> Locale? {
        if let match = await SpeechTranscriber.supportedLocale(equivalentTo: Locale.current) {
            return match
        }
        let supported = await SpeechTranscriber.supportedLocales
        return supported.first { $0.identifier.hasPrefix("en") } ?? supported.first
    }

    private func begin() async {
        guard let modules = await Self.makeModules() else {
            DispatchQueue.main.async {
                self.reportFailure("This Mac has no on-device transcription model available.")
            }
            return
        }
        DispatchQueue.main.async {
            Settings.localeIdentifier = modules.locale.identifier
            // Enough to prove the engine exists — AppDelegate treats it the way
            // it treats the server's first word, and the HUD stops saying
            // "Connecting…".
            self.onConnected?()
        }

        do {
            try await ensureAssets(for: modules)
            guard !(await isClosed()) else { return }

            let format = await SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith: modules.all)
            formatBox.withLock { $0 = format }

            let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
            continuationBox.withLock { $0 = continuation }

            let analyzer = SpeechAnalyzer(modules: modules.all,
                                          options: .init(priority: .userInitiated,
                                                         modelRetention: .processLifetime))
            let consumers = startConsuming(modules)
            try await analyzer.start(inputSequence: stream)

            DispatchQueue.main.async {
                // A close() that landed while the analyzer was starting has
                // already run its teardown against nothing, so this dictation's
                // parts have to tear themselves down rather than being stored.
                guard !self.closed else {
                    consumers.results.cancel()
                    consumers.vad.cancel()
                    Task { await analyzer.cancelAndFinishNow() }
                    return
                }
                self.analyzer = analyzer
                self.resultsTask = consumers.results
                self.vadTask = consumers.vad
                self.onInfo?("ready", "Apple Speech (\(modules.locale.identifier))")
            }
        } catch {
            DispatchQueue.main.async { self.reportFailure(error.localizedDescription) }
        }
    }

    /// Make sure the language model for this locale is on disk, downloading it
    /// if the framework says it can. Reported as "loading" so the HUD's existing
    /// placeholder covers the wait.
    private func ensureAssets(for modules: Modules) async throws {
        switch await AssetInventory.status(forModules: modules.all) {
        case .installed:
            return
        case .unsupported:
            throw LocalEngineError.unsupportedLocale(modules.locale.identifier)
        case .supported, .downloading:
            DispatchQueue.main.async { self.onInfo?("loading", "Apple Speech") }
            if let request = try await AssetInventory.assetInstallationRequest(supporting: modules.all) {
                try await request.downloadAndInstall()
            }
            // Ask macOS to keep it resident rather than reclaiming it between
            // dictations. Best-effort: there is a cap on reserved locales, and
            // being over it is not a reason to fail the dictation.
            _ = try? await AssetInventory.reserve(locale: modules.locale)
        @unknown default:
            return
        }
    }

    /// Drain both modules' result streams for this dictation. Returns the two
    /// tasks rather than storing them: this runs off the main queue, and the
    /// handles are torn down from `stop()` and `close()` on main.
    private func startConsuming(_ modules: Modules)
        -> (results: Task<Void, Never>, vad: Task<Void, Never>) {
        let results = Task { [weak self] in
            do {
                for try await result in modules.transcriber.results {
                    guard let self, !Task.isCancelled else { return }
                    let text = String(result.text.characters)
                    let isFinal = result.isFinal
                    DispatchQueue.main.async {
                        guard !self.closed else { return }
                        if isFinal {
                            // Settled: it moves behind the live text and stops
                            // being revised.
                            self.committed = Self.join(self.committed, text)
                            self.live = ""
                        } else {
                            self.live = text
                        }
                        self.onPartial?(self.committed, self.live)
                    }
                }
            } catch {
                guard let self, !Task.isCancelled else { return }
                DispatchQueue.main.async { self.reportFailure(error.localizedDescription) }
            }
        }

        let vad = Task { [weak self] in
            do {
                for try await result in modules.detector.results {
                    guard let self, !Task.isCancelled else { return }
                    let speech = result.speechDetected
                    DispatchQueue.main.async {
                        guard !self.closed else { return }
                        self.onVad?(speech)
                    }
                }
            } catch {
                // The transcript is what matters; losing the meter's speech
                // hint is not worth failing a dictation over.
            }
        }
        return (results, vad)
    }

    // MARK: helpers

    private func isClosed() async -> Bool {
        await MainActor.run { self.closed }
    }

    /// Audio thread. AudioCapture is configured from `inputFormat`, so this is
    /// normally a no-op — it only does work if a dictation started before
    /// `prewarm()` had negotiated the real format.
    private func convert(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        let target = inputFormat
        if buffer.format == target { return buffer }
        if fixup?.inputFormat != buffer.format || fixup?.outputFormat != target {
            fixup = AVAudioConverter(from: buffer.format, to: target)
        }
        guard let fixup else { return nil }
        let ratio = target.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 32)
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return nil }
        var fed = false
        var error: NSError?
        let status = fixup.convert(to: out, error: &error) { _, inputStatus in
            if fed { inputStatus.pointee = .noDataNow; return nil }
            fed = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status == .haveData || status == .inputRanDry, out.frameLength > 0 else { return nil }
        return out
    }

    /// Stop waiting on a transcript that isn't coming. Main queue only.
    private func armFinalizeWatchdog() {
        finalizeWatchdog?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self, !self.closed else { return }
            self.close()
            self.onFinalizeTimeout?()
        }
        finalizeWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.finalizeTimeout, execute: work)
    }

    /// Report once and tear down. Main queue only. Routed to `onError` rather
    /// than `onUnreachable`: whatever went wrong here, "start your server" is
    /// not the fix.
    private func reportFailure(_ message: String) {
        guard !reportedFailure, !closed else { return }
        reportedFailure = true
        close()
        onError?(message)
    }

    private static func join(_ a: String, _ b: String) -> String {
        guard !a.isEmpty else { return b }
        guard !b.isEmpty else { return a }
        return a + " " + b
    }
}

@available(macOS 26, *)
private enum LocalEngineError: LocalizedError {
    case unsupportedLocale(String)

    var errorDescription: String? {
        switch self {
        case .unsupportedLocale(let id):
            return "macOS has no on-device transcription model for \(id). "
                 + "Switch to server mode in Settings, or change your Mac's language."
        }
    }
}
