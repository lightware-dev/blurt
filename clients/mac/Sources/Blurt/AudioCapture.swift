import AVFoundation

/// Captures the default mic and emits 16 kHz mono Int16 PCM frames (little-endian),
/// exactly what the server's WebSocket expects. Resampling is done by AVAudioConverter.
final class AudioCapture {
    private let engine = AVAudioEngine()
    private var converter: AVAudioConverter?
    private let outFormat = AVAudioFormat(commonFormat: .pcmFormatInt16,
                                          sampleRate: 16000, channels: 1, interleaved: true)!
    /// Called on an audio thread with a chunk of Int16 PCM bytes.
    var onFrame: ((Data) -> Void)?
    /// Called on an audio thread with per-band FFT magnitudes (0…1) of each
    /// chunk, for the HUD's spectrum meter.
    var onSpectrum: (([Float]) -> Void)?
    private let spectrum = Spectrum(bandCount: 24)

    func start() throws {
        let input = engine.inputNode
        let inFormat = input.inputFormat(forBus: 0)
        converter = AVAudioConverter(from: inFormat, to: outFormat)
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
        let ratio = outFormat.sampleRate / buffer.format.sampleRate
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio + 32)
        guard let out = AVAudioPCMBuffer(pcmFormat: outFormat, frameCapacity: capacity) else { return }

        var fed = false
        var error: NSError?
        let status = converter.convert(to: out, error: &error) { _, inputStatus in
            if fed { inputStatus.pointee = .noDataNow; return nil }
            fed = true
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status == .haveData || status == .inputRanDry,
              let ch = out.int16ChannelData, out.frameLength > 0 else { return }
        let count = Int(out.frameLength)
        let data = Data(bytes: ch[0], count: count * MemoryLayout<Int16>.size)
        onFrame?(data)

        if let onSpectrum = onSpectrum {
            onSpectrum(spectrum.ingest(ch[0], count: count))
        }
    }
}
