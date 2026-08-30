import AVFoundation
import Speech

/// What the app needs from something that turns speech into text, whichever end
/// of the wire it lives on. Both engines report through the same callbacks, so
/// AppDelegate wires itself once and doesn't care which one it got.
///
/// The callbacks are deliberately the ones DictationClient already had — the
/// server's protocol shaped this surface, and Apple's on-device API happens to
/// fit it almost exactly (volatile results are `live`, finalized ones accumulate
/// into `committed`). All of them fire on the main queue.
protocol TranscriptionEngine: AnyObject {
    var onPartial: ((String, String) -> Void)? { get set }   // (committed, live)
    var onFinal: ((String) -> Void)? { get set }
    var onVad: ((Bool) -> Void)? { get set }                 // is it hearing speech?
    var onInfo: ((String, String) -> Void)? { get set }      // (state "ready|loading", model)
    var onStatus: ((String, String?) -> Void)? { get set }   // (state, detail)
    var onError: ((String) -> Void)? { get set }
    /// Proof the engine is actually able to transcribe. Until this fires the HUD
    /// says "Connecting…" rather than claiming to listen.
    var onConnected: (() -> Void)? { get set }
    /// The engine never got as far as being usable, with an actionable cause.
    /// Only the server engine fires this; there is nothing to be unreachable
    /// about on-device.
    var onUnreachable: ((String) -> Void)? { get set }
    /// `stop()` went unanswered past the engine's own budget, so the finished
    /// transcript is not coming.
    var onFinalizeTimeout: (() -> Void)? { get set }

    /// The PCM format this engine wants from AudioCapture. The server takes the
    /// 16 kHz Int16 its protocol declares; SpeechAnalyzer names its own.
    var inputFormat: AVAudioFormat { get }

    /// Warm up ahead of the first dictation, so loading a model isn't what the
    /// user's first sentence pays for. Best-effort: never blocks recording.
    func prewarm()

    func connectAndStart()
    func sendAudio(_ buffer: AVAudioPCMBuffer)
    /// Ask for the finished transcript; the engine replies once via `onFinal`.
    func stop()
    /// Tear down without finalizing — nothing gets injected.
    func close()
}

extension TranscriptionEngine {
    func prewarm() {}
}

/// Whether this Mac can transcribe on its own.
///
/// `SpeechTranscriber.isAvailable` answers both halves of the question at once —
/// the API only exists on macOS 26, and even there it reports false without the
/// Neural Engine, which is to say on every Intel Mac. Verified on a
/// MacBookPro16,2 running macOS 26.5: `isAvailable` is false, `supportedLocales`
/// is empty and AssetInventory calls the modules unsupported. So there is no
/// need to sniff the architecture ourselves, and no `#if arch(...)` anywhere —
/// the symbols ship in the x86_64 slice of the SDK too, so the universal build
/// compiles unchanged and simply takes the server path at runtime.
enum LocalTranscription {
    static var isSupported: Bool {
        guard #available(macOS 26, *) else { return false }
        return SpeechTranscriber.isAvailable
    }

    /// Why the local engine is unavailable, for the Settings window to explain
    /// itself. Nil when it is available.
    static var unavailableReason: String? {
        guard #available(macOS 26, *) else { return "Needs macOS 26 or later." }
        guard !SpeechTranscriber.isAvailable else { return nil }
        return "Needs a Mac with Apple silicon."
    }
}
