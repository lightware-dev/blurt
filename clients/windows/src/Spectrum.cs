namespace Blurt;

/// Turns 16 kHz mono Int16 PCM into a handful of log-spaced frequency-band
/// magnitudes (0…1) for the HUD's spectrum meter — the twin of the Mac client's
/// Spectrum.swift. A voice-shaped FFT rather than a single volume level. Keeps a
/// sliding window of the most recent `_fftSize` samples so each call reflects the
/// latest ~32 ms of audio. macOS leans on Accelerate/vDSP; here we carry a small
/// self-contained radix-2 FFT so there's no extra dependency.
internal sealed class Spectrum
{
    private readonly int _fftSize;
    private readonly int _bandCount;
    private readonly float[] _window;
    private readonly float[] _ring;
    private readonly (int Lo, int Hi)[] _bandBins;

    // Scratch buffers for the in-place FFT, reused every frame.
    private readonly float[] _re;
    private readonly float[] _im;

    // Per-band auto-gain: each band normalises against its own recent peak (dB),
    // which decays slowly. Naturally-quiet high bands then fill just as much as
    // the loud low bands when active, so activity spreads across the whole width
    // instead of piling up on the left — without any left/right mirroring.
    private readonly float[] _peakDb;
    private const float PeakDecayDb = 0.6f;   // how fast a band's peak relaxes, per frame
    private const float MinSpanDb = 20f;      // smallest peak-to-gate range, keeps quiet bands calm

    // Per-band noise gate: a minimum-follower tracks each band's ambient level,
    // and only energy comfortably above that ambient moves the meter. Without
    // this the auto-gain amplifies room hiss to half-scale whenever it's quiet.
    // Both directions are rate-limited: slow up so speech doesn't get absorbed
    // into the ambient estimate, and bounded down so a single-frame spectral
    // null (narrow bands dip to -100 dB at random) can't drag the gate through
    // the floor and let ordinary noise "clear" it.
    private readonly float[] _noiseDb;
    private const float NoiseRiseDb = 0.05f;  // ambient estimate creep-up, per frame
    private const float NoiseFallDb = 1.2f;   // ambient estimate drop, per frame
    private const float GateDb = 12f;         // signal must clear ambient by this much
    // The estimate starts at 0 dB ("assume loud") and can only descend at the
    // bounded rate — from there to room level takes seconds, during which the
    // gate freezes the meter on the first session after launch. So during a
    // short warmup the estimate instead snaps straight down to the quietest
    // real level seen per band (digital silence from a still-warming mic,
    // -140 dB, doesn't count), and gating meanwhile assumes ambient is no
    // louder than -60 dB so speech registers even if the user talks from the
    // very first frame.
    private int _warmupFrames = 30;           // ~1.2 s of real signal

    public Spectrum(int fftSize = 512, int bandCount = 24, double sampleRate = 16000)
    {
        _fftSize = fftSize;
        _bandCount = bandCount;
        _window = new float[fftSize];
        // Hann window, matching vDSP_hann_window on the Mac.
        for (int i = 0; i < fftSize; i++)
            _window[i] = (float)(0.5 * (1 - Math.Cos(2 * Math.PI * i / (fftSize - 1))));
        _ring = new float[fftSize];
        _re = new float[fftSize];
        _im = new float[fftSize];

        _peakDb = new float[bandCount];
        Array.Fill(_peakDb, -50f);            // -70 + 20, as on the Mac
        // Start high: the follower drops instantly, so it locks onto the real
        // ambient level within the first frame instead of creeping up from -∞.
        _noiseDb = new float[bandCount];      // zeros = "assume loud"

        // Log-spaced band edges across the voice range, mapped to FFT bins.
        const double lowHz = 80.0, highHz = 5000.0;
        double binHz = sampleRate / fftSize;
        int maxBin = fftSize / 2 - 1;
        _bandBins = new (int, int)[bandCount];
        for (int b = 0; b < bandCount; b++)
        {
            double f0 = lowHz * Math.Pow(highHz / lowHz, (double)b / bandCount);
            double f1 = lowHz * Math.Pow(highHz / lowHz, (double)(b + 1) / bandCount);
            int lo = Math.Min(Math.Max((int)(f0 / binHz), 1), maxBin);
            int hi = Math.Min(Math.Max((int)(f1 / binHz), lo + 1), maxBin + 1);
            _bandBins[b] = (lo, hi);
        }
    }

    /// Slide the new Int16 samples in `data` (little-endian, `byteCount` bytes)
    /// into the window and return band magnitudes.
    public float[] Ingest(byte[] data, int byteCount)
    {
        int count = byteCount / 2;
        if (count >= _fftSize)
        {
            int offset = count - _fftSize;
            for (int i = 0; i < _fftSize; i++) _ring[i] = ReadSample(data, offset + i);
        }
        else
        {
            int keep = _fftSize - count;
            Array.Copy(_ring, count, _ring, 0, keep);
            for (int i = 0; i < count; i++) _ring[keep + i] = ReadSample(data, i);
        }
        return Analyze();
    }

    private static float ReadSample(byte[] d, int sampleIndex)
    {
        int i = sampleIndex * 2;
        short s = (short)(d[i] | (d[i + 1] << 8));  // little-endian PCM16
        return s / 32768f;
    }

    private float[] Analyze()
    {
        for (int i = 0; i < _fftSize; i++)
        {
            _re[i] = _ring[i] * _window[i];
            _im[i] = 0f;
        }
        Fft(_re, _im);

        float norm = 1f / _fftSize;
        var outv = new float[_bandCount];
        bool sawSignal = false;
        for (int b = 0; b < _bandCount; b++)
        {
            var (lo, hi) = _bandBins[b];
            float sum = 0f;
            for (int i = lo; i < hi; i++)
                sum += MathF.Sqrt(_re[i] * _re[i] + _im[i] * _im[i]) * norm;
            float avg = sum / Math.Max(hi - lo, 1);
            float db = 20f * MathF.Log10(avg + 1e-7f);

            // Track ambient: descend toward quieter frames, creep up otherwise.
            if (_warmupFrames > 0)
            {
                if (db > -120f) { _noiseDb[b] = Math.Min(_noiseDb[b], db); sawSignal = true; }
            }
            else
            {
                _noiseDb[b] = db < _noiseDb[b]
                    ? Math.Max(db, _noiseDb[b] - NoiseFallDb)
                    : _noiseDb[b] + NoiseRiseDb;
            }
            float ambient = _warmupFrames > 0 ? Math.Min(_noiseDb[b], -60f) : _noiseDb[b];
            float gate = ambient + GateDb;
            // Relax this band's peak, then let the current level catch it.
            _peakDb[b] = Math.Max(db, _peakDb[b] - PeakDecayDb);
            float span = Math.Max(_peakDb[b] - gate, MinSpanDb);
            float level = Math.Min(Math.Max((db - gate) / span, 0f), 1f);
            // Mild expander: squashes low-level flutter near the gate while
            // leaving real speech swings nearly untouched.
            outv[b] = level * level * (3 - 2 * level);
        }
        if (sawSignal) _warmupFrames--;
        return outv;
    }

    /// In-place iterative radix-2 Cooley-Tukey FFT (forward). `re`/`im` length
    /// must be a power of two. On return each bin holds its complex spectrum;
    /// callers take the magnitude of the first half.
    private static void Fft(float[] re, float[] im)
    {
        int n = re.Length;
        // Bit-reversal permutation.
        for (int i = 1, j = 0; i < n; i++)
        {
            int bit = n >> 1;
            for (; (j & bit) != 0; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j)
            {
                (re[i], re[j]) = (re[j], re[i]);
                (im[i], im[j]) = (im[j], im[i]);
            }
        }
        // Butterflies, doubling the transform length each stage.
        for (int len = 2; len <= n; len <<= 1)
        {
            double ang = -2 * Math.PI / len;
            float wr = (float)Math.Cos(ang), wi = (float)Math.Sin(ang);
            for (int i = 0; i < n; i += len)
            {
                float cr = 1f, ci = 0f;
                for (int k = 0; k < len / 2; k++)
                {
                    int a = i + k, c = a + len / 2;
                    float tr = cr * re[c] - ci * im[c];
                    float ti = cr * im[c] + ci * re[c];
                    re[c] = re[a] - tr; im[c] = im[a] - ti;
                    re[a] += tr; im[a] += ti;
                    float ncr = cr * wr - ci * wi;
                    ci = cr * wi + ci * wr;
                    cr = ncr;
                }
            }
        }
    }
}
