using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Media.Effects;
using System.Windows.Shapes;
using System.Windows.Threading;

namespace Blurt;

/// Flowing layered waves for the HUD — the twin of the Mac client's Waveform.swift.
/// Three smooth sine ribbons drift horizontally and swell with the voice, in the
/// style of a glowing audio ribbon. The front ribbon is the brightest — Blurt's
/// highlighter yellow with a soft glow — and the ribbons behind it are progressively
/// darker and thinner, so intensity reads as brightness. Each ribbon is driven by a
/// different slice of the live FFT (lows / mids / highs), so the layers move
/// independently rather than in lockstep. Swells snap up on attack and ease down on
/// decay. Rendered as three filled Path children of a Canvas, refreshed at 60 fps.
internal sealed class WaveformView : Canvas
{
    // Ribbon look, back to front. Darker ribbons sit behind the bright one.
    private readonly struct Ribbon
    {
        public readonly Brush Brush;
        public readonly double Width;   // stroke width in DIPs at full end-fade
        public readonly double Amp;     // share of the view's half-height at full level
        public readonly double Cycles;  // spatial frequency across the width
        public readonly double Cycles2; // second harmonic, keeps the shape organic
        public readonly double Speed;   // phase speed (negative drifts the other way)
        public readonly double Offset;  // phase offset so ribbons never align
        public readonly bool Glow;

        public Ribbon(Brush brush, double width, double amp, double cycles,
                      double cycles2, double speed, double offset, bool glow)
        {
            Brush = brush; Width = width; Amp = amp; Cycles = cycles;
            Cycles2 = cycles2; Speed = speed; Offset = offset; Glow = glow;
        }
    }

    private static readonly Ribbon[] Ribbons =
    {
        new(Frozen(Blend(Brand.Marker, Colors.Black, 0.62)),
            0.7, 0.95, 2.6, 4.1, -0.7, 2.1, false),
        new(Frozen(Blend(Brand.Marker, Colors.Black, 0.35)),
            1.1, 0.85, 2.1, 3.3, 1.25, 4.4, false),
        new(Frozen(Blend(Brand.Marker, Colors.White, 0.25)),
            1.7, 0.75, 1.6, 2.7, 0.9, 0.0, true),
    };

    private readonly Path[] _paths;
    // One entry per ribbon: smoothed level (0…1) and its FFT-driven target.
    private readonly double[] _levels = { 0, 0, 0 };
    private readonly double[] _targets = { 0, 0, 0 };
    private double _phase;
    private DispatcherTimer? _timer;
    private bool _active;

    public WaveformView()
    {
        // No ClipToBounds: let the front ribbon's soft glow bloom past the 96×34
        // box (the ink pill has room), exactly as the Mac's shadow does.
        _paths = new Path[Ribbons.Length];
        for (int i = 0; i < Ribbons.Length; i++)
        {
            var p = new Path { Fill = Ribbons[i].Brush };
            if (Ribbons[i].Glow)
                p.Effect = new DropShadowEffect
                {
                    Color = Brand.Marker,
                    BlurRadius = 8,
                    ShadowDepth = 0,
                    Opacity = 0.9,
                };
            _paths[i] = p;
            Children.Add(p); // added back-to-front, so the glow ribbon paints last
        }
    }

    /// Feed the latest frequency-band magnitudes (0…1). The bands are split into
    /// three groups — highs drive the back ribbon, mids the middle, lows the
    /// bright front one (the voice fundamental, usually the strongest signal).
    /// Must be called on the UI thread.
    public void SetBands(float[] values)
    {
        if (values.Length == 0) return;
        int third = Math.Max(values.Length / 3, 1);
        _targets[2] = Group(values, 0, third);                                 // lows → front
        _targets[1] = Group(values, third, Math.Min(2 * third, values.Length));
        _targets[0] = Group(values, Math.Min(2 * third, values.Length), values.Length); // highs → back
        _active = true;
        if (_timer is null) StartAnimating();
    }

    private static double Group(float[] v, int lo, int hi)
    {
        if (hi <= lo) return 0;
        float sum = 0, peak = 0;
        for (int i = lo; i < hi; i++) { sum += v[i]; if (v[i] > peak) peak = v[i]; }
        float mean = sum / (hi - lo);
        // Blend mean and peak: mean alone is sluggish, peak alone is jittery.
        return Math.Min(Math.Max(0.6 * mean + 0.4 * peak, 0), 1);
    }

    /// Flatten the waves and stop animating — used when a session ends.
    public void Reset()
    {
        _timer?.Stop();
        _timer = null;
        _active = false;
        for (int i = 0; i < _levels.Length; i++)
        {
            _levels[i] = 0;
            _targets[i] = 0;
            _paths[i].Data = null;
        }
    }

    private void StartAnimating()
    {
        _timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1.0 / 60.0) };
        _timer.Tick += (_, _) => Tick();
        _timer.Start();
    }

    private void Tick()
    {
        _phase += 0.09;
        for (int i = 0; i < _levels.Length; i++)
        {
            double a = _targets[i] > _levels[i] ? 0.45 : 0.12; // fast attack, slow decay
            _levels[i] += (_targets[i] - _levels[i]) * a;
        }
        Render();
    }

    private void Render()
    {
        if (!_active) return;
        double w = ActualWidth, h = ActualHeight;
        if (w <= 0 || h <= 0) return;
        double midY = h / 2;
        double halfH = h / 2 - 1;
        const double step = 1.5;
        const double fadeSpan = 0.18; // fraction of the width over which the ends dissolve

        for (int r = 0; r < Ribbons.Length; r++)
        {
            var rib = Ribbons[r];
            // A faint ripple stays even in silence so the HUD reads as listening.
            double level = Math.Max(_levels[r], 0.06);
            double amp = rib.Amp * level * halfH;

            // Sample the wave, tagging each point with its end-fade factor.
            int cap = (int)(w / step) + 2;
            var px = new double[cap];
            var py = new double[cap];
            var fade = new double[cap];
            int n = 0;
            for (double x = 0; x <= w; x += step)
            {
                double t = x / w;
                // Taper toward both ends so the ribbons converge, as in a ribbon of light.
                double envelope = Math.Pow(Math.Sin(Math.PI * t), 1.3);
                double s1 = Math.Sin(t * 2 * Math.PI * rib.Cycles + _phase * rib.Speed + rib.Offset);
                double s2 = Math.Sin(t * 2 * Math.PI * rib.Cycles2 - _phase * rib.Speed * 1.4 + rib.Offset * 1.7);
                double y = midY + amp * envelope * (0.62 * s1 + 0.38 * s2);
                double edge = Math.Min(Math.Max(Math.Min(t, 1 - t) / fadeSpan, 0), 1);
                px[n] = x; py[n] = y; fade[n] = edge * edge * (3 - 2 * edge);
                n++;
            }

            // Build the ribbon as one filled variable-width shape: offset each
            // sample along the curve normal by half the local width, walk the top
            // edge out and the bottom edge back. The width tapers with the end
            // fade, so the tips converge to a point and dissolve smoothly instead
            // of stopping dead at the component edge — with no seams.
            var top = new Point[n];
            var bottom = new Point[n];
            for (int j = 0; j < n; j++)
            {
                int pj = Math.Max(j - 1, 0), nj = Math.Min(j + 1, n - 1);
                double dx = px[nj] - px[pj], dy = py[nj] - py[pj];
                double len = Math.Max(Math.Sqrt(dx * dx + dy * dy), 0.001);
                double halfW = Math.Max(rib.Width * fade[j], 0.05) / 2;
                double nx = -dy / len * halfW, ny = dx / len * halfW;
                top[j] = new Point(px[j] + nx, py[j] + ny);
                bottom[j] = new Point(px[j] - nx, py[j] - ny);
            }

            var geo = new StreamGeometry();
            using (var gc = geo.Open())
            {
                gc.BeginFigure(top[0], true, true);
                gc.PolyLineTo(top[1..n], true, false);
                var bottomRev = new Point[n];
                for (int j = 0; j < n; j++) bottomRev[j] = bottom[n - 1 - j];
                gc.PolyLineTo(bottomRev, true, false);
            }
            geo.Freeze();
            _paths[r].Data = geo;
        }
    }

    // ── colour helpers ──────────────────────────────────────
    private static Color Blend(Color c, Color other, double f) => Color.FromRgb(
        (byte)Math.Round(c.R + (other.R - c.R) * f),
        (byte)Math.Round(c.G + (other.G - c.G) * f),
        (byte)Math.Round(c.B + (other.B - c.B) * f));

    private static Brush Frozen(Color c)
    {
        var b = new SolidColorBrush(c);
        b.Freeze();
        return b;
    }
}
