import Accelerate

/// Turns 16 kHz mono Int16 PCM into a handful of log-spaced frequency-band
/// magnitudes (0…1) for the HUD's spectrum meter — a voice-shaped FFT rather
/// than a single volume level. Keeps a sliding window of the most recent
/// `fftSize` samples so each call reflects the latest ~32 ms of audio.
final class Spectrum {
    private let fftSize: Int
    private let bandCount: Int
    private let log2n: vDSP_Length
    private let setup: FFTSetup
    private var window: [Float]
    private var ring: [Float]
    private var realp: [Float]
    private var imagp: [Float]
    private let bandBins: [(lo: Int, hi: Int)]

    // Per-band auto-gain: each band normalises against its own recent peak (dB),
    // which decays slowly. Naturally-quiet high bands then fill just as much as
    // the loud low bands when active, so activity spreads across the whole width
    // instead of piling up on the left — without any left/right mirroring.
    private var peakDb: [Float]
    private let peakDecayDb: Float = 0.6        // how fast a band's peak relaxes, per frame
    private let minSpanDb: Float = 20           // smallest peak-to-gate range, keeps quiet bands calm

    // Per-band noise gate: a minimum-follower tracks each band's ambient level,
    // and only energy comfortably above that ambient moves the meter. Without
    // this the auto-gain amplifies room hiss to half-scale whenever it's quiet.
    // Both directions are rate-limited: slow up so speech doesn't get absorbed
    // into the ambient estimate, and bounded down so a single-frame spectral
    // null (narrow bands dip to -100 dB at random) can't drag the gate through
    // the floor and let ordinary noise "clear" it.
    private var noiseDb: [Float]
    private let noiseRiseDb: Float = 0.05       // ambient estimate creep-up, per frame
    private let noiseFallDb: Float = 1.2        // ambient estimate drop, per frame
    private let gateDb: Float = 12              // signal must clear ambient by this much
    // The estimate starts at 0 dB ("assume loud") and can only descend at the
    // bounded rate — from there to room level takes seconds, during which the
    // gate freezes the meter on the first session after launch. So during a
    // short warmup the estimate instead snaps straight down to the quietest
    // real level seen per band (digital silence from a still-warming mic,
    // -140 dB, doesn't count), and gating meanwhile assumes ambient is no
    // louder than -60 dB so speech registers even if the user talks from the
    // very first frame.
    private var warmupFrames = 30               // ~1.2 s of real signal

    init(fftSize: Int = 512, bandCount: Int = 22, sampleRate: Double = 16000) {
        self.fftSize = fftSize
        self.bandCount = bandCount
        self.log2n = vDSP_Length(log2(Double(fftSize)))
        self.setup = vDSP_create_fftsetup(log2n, FFTRadix(kFFTRadix2))!
        self.window = [Float](repeating: 0, count: fftSize)
        vDSP_hann_window(&window, vDSP_Length(fftSize), Int32(vDSP_HANN_NORM))
        self.ring = [Float](repeating: 0, count: fftSize)
        self.realp = [Float](repeating: 0, count: fftSize / 2)
        self.imagp = [Float](repeating: 0, count: fftSize / 2)
        self.peakDb = [Float](repeating: -70 + 20, count: bandCount)
        // Start high: the follower drops instantly, so it locks onto the real
        // ambient level within the first frame instead of creeping up from -∞.
        self.noiseDb = [Float](repeating: 0, count: bandCount)

        // Log-spaced band edges across the voice range, mapped to FFT bins.
        let lowHz = 80.0, highHz = 5000.0
        let binHz = sampleRate / Double(fftSize)
        let maxBin = fftSize / 2 - 1
        var bins = [(Int, Int)]()
        for b in 0..<bandCount {
            let f0 = lowHz * pow(highHz / lowHz, Double(b) / Double(bandCount))
            let f1 = lowHz * pow(highHz / lowHz, Double(b + 1) / Double(bandCount))
            let lo = min(max(Int(f0 / binHz), 1), maxBin)
            let hi = min(max(Int(f1 / binHz), lo + 1), maxBin + 1)
            bins.append((lo, hi))
        }
        self.bandBins = bins
    }

    deinit { vDSP_destroy_fftsetup(setup) }

    /// Slide `count` new Int16 samples into the window and return band magnitudes.
    func ingest(_ samples: UnsafePointer<Int16>, count: Int) -> [Float] {
        let scale = Float(Int16.max)
        if count >= fftSize {
            for i in 0..<fftSize { ring[i] = Float(samples[count - fftSize + i]) / scale }
        } else {
            let keep = fftSize - count
            for i in 0..<keep { ring[i] = ring[i + count] }
            for i in 0..<count { ring[keep + i] = Float(samples[i]) / scale }
        }
        return analyze()
    }

    /// Float32 sibling, for engines that ask for float PCM (SpeechAnalyzer does).
    /// Already in -1…1, so unlike the Int16 path there is nothing to normalize.
    func ingest(_ samples: UnsafePointer<Float>, count: Int) -> [Float] {
        if count >= fftSize {
            for i in 0..<fftSize { ring[i] = samples[count - fftSize + i] }
        } else {
            let keep = fftSize - count
            for i in 0..<keep { ring[i] = ring[i + count] }
            for i in 0..<count { ring[keep + i] = samples[i] }
        }
        return analyze()
    }

    private func analyze() -> [Float] {
        let half = fftSize / 2
        var windowed = [Float](repeating: 0, count: fftSize)
        vDSP_vmul(ring, 1, window, 1, &windowed, 1, vDSP_Length(fftSize))

        var mags = [Float](repeating: 0, count: half)
        realp.withUnsafeMutableBufferPointer { rp in
            imagp.withUnsafeMutableBufferPointer { ip in
                var split = DSPSplitComplex(realp: rp.baseAddress!, imagp: ip.baseAddress!)
                windowed.withUnsafeBufferPointer { wp in
                    wp.baseAddress!.withMemoryRebound(to: DSPComplex.self, capacity: half) { cp in
                        vDSP_ctoz(cp, 2, &split, 1, vDSP_Length(half))
                    }
                }
                vDSP_fft_zrip(setup, &split, 1, log2n, FFTDirection(FFT_FORWARD))
                vDSP_zvabs(&split, 1, &mags, 1, vDSP_Length(half))
            }
        }
        var norm = 1 / Float(fftSize)
        vDSP_vsmul(mags, 1, &norm, &mags, 1, vDSP_Length(half))

        var out = [Float](repeating: 0, count: bandCount)
        var sawSignal = false
        for (b, range) in bandBins.enumerated() {
            var sum: Float = 0
            for i in range.lo..<range.hi { sum += mags[i] }
            let avg = sum / Float(max(range.hi - range.lo, 1))
            let db = 20 * log10(avg + 1e-7)
            // Track ambient: descend toward quieter frames, creep up otherwise.
            if warmupFrames > 0 {
                if db > -120 { noiseDb[b] = min(noiseDb[b], db); sawSignal = true }
            } else {
                noiseDb[b] = db < noiseDb[b] ? max(db, noiseDb[b] - noiseFallDb)
                                             : noiseDb[b] + noiseRiseDb
            }
            let ambient = warmupFrames > 0 ? min(noiseDb[b], -60) : noiseDb[b]
            let gate = ambient + gateDb
            // Relax this band's peak, then let the current level catch it.
            peakDb[b] = max(db, peakDb[b] - peakDecayDb)
            let span = max(peakDb[b] - gate, minSpanDb)
            let level = min(max((db - gate) / span, 0), 1)
            // Mild expander: squashes low-level flutter near the gate while
            // leaving real speech swings nearly untouched.
            out[b] = level * level * (3 - 2 * level)
        }
        if sawSignal { warmupFrames -= 1 }
        return out
    }
}
