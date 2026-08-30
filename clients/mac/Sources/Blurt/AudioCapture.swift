import AVFoundation

/// Captures the default mic and emits mono PCM frames in whatever format the
/// active engine asked for. The server wants the 16 kHz Int16 its protocol
/// declares; SpeechAnalyzer names its own via `bestAvailableAudioFormat`, which
/// is Float32 in practice. Resampling is done by AVAudioConverter either way.
final class AudioCapture {
    /// 16 kHz mono Int16 little-endian — the server's wire format, and the
    /// default until an engine says otherwise.
    static let serverFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                            sampleRate: 16000, channels: 1, interleaved: true)!

    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?

    /// The format frames are delivered in. Set before `start()`; changing it
    /// while running has no effect until the next start.
    var outputFormat: AVAudioFormat = AudioCapture.serverFormat

    /// Called on an audio thread with a chunk of converted PCM.
    var onFrame: ((AVAudioPCMBuffer) -> Void)?
    /// Called on an audio thread with per-band FFT magnitudes (0…1) of each
    /// chunk, for the HUD's spectrum meter.
    var onSpectrum: (([Float]) -> Void)?
    /// Rebuilt on each start, because its band edges depend on the sample rate
    /// the active engine chose.
    private var spectrum = Spectrum(bandCount: 24)

    func start() throws {
        let input = engine.inputNode
        let inFormat = input.inputFormat(forBus: 0)
        converter = AVAudioConverter(from: inFormat, to: outputFormat)
        spectrum = Spectrum(bandCount: 24, sampleRate: outputFormat.sampleRate)
        input.installTap(onBus: 0, bufferSize: 2048, format: inFormat) { [weak self] buffer, _ in
            self?.handle(buffer)
        }
        engine.prepare()
        try engine.start()
    }

    func stop() {
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
    }

    private func handle(_ buffer: AVAudioPCMBuffer) {
        guard let converter = converter else { return }
        let ratio = outputFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 32)
        guard let out = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { return }

        var fed = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if fed { inputStatus.pointee = .noDataNow; return nil }
            fed = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status == .haveData || status == .inputRanDry, out.frameLength > 0 else { return }
        onFrame?(out)

        if let onSpectrum = onSpectrum {
            let count = Int(out.frameLength)
            // Whichever sample type the engine asked for; the meter is the same
            // either way, Spectrum just normalizes differently.
            if let ch = out.int16ChannelData {
                onSpectrum(spectrum.ingest(ch[0], count: count))
            } else if let ch = out.floatChannelData {
                onSpectrum(spectrum.ingest(ch[0], count: count))
            }
        }
    }
}
