import AppKit

/// Flowing layered waves for the HUD, in the style of a glowing audio ribbon:
/// three smooth sine ribbons drift horizontally and swell with the voice. The
/// front ribbon is the brightest — Blurt's highlighter yellow with a soft glow —
/// and the ribbons behind it are progressively darker and thinner, so intensity
/// reads as brightness. Each ribbon is driven by a different slice of the live
/// FFT (lows / mids / highs), so the layers move independently rather than in
/// lockstep. Swells snap up on attack and ease down on decay.
final class WaveformView: NSView {
    // One entry per ribbon: smoothed level (0…1) and its FFT-driven target.
    private var levels: [CGFloat] = [0, 0, 0]
    private var targets: [CGFloat] = [0, 0, 0]
    private var phase: CGFloat = 0
    private var timer: Timer?
    private var active = false

    // Server-side VAD presence, smoothed 0…1. The ribbons are driven by the
    // *local* mic, so they ripple identically whether the server is listening
    // or the connection died mid-dictation. Brightness is what distinguishes
    // the two: full strength once the server confirms it hears speech, muted
    // (never invisible — the local meter still has something to say) when it
    // reports silence. That makes the whole mic → network → server path
    // legible at a glance, which the FFT alone cannot show.
    private var hearing: CGFloat = 0
    private var hearingTarget: CGFloat = 0
    private static let dimFloor: CGFloat = 0.4

    // Ribbon look, back to front. Darker ribbons sit behind the bright one.
    private struct Ribbon {
        let color: NSColor
        let width: CGFloat
        let amp: CGFloat          // share of the view's half-height at full level
        let cycles: CGFloat       // spatial frequency across the width
        let cycles2: CGFloat      // second harmonic, keeps the shape organic
        let speed: CGFloat        // phase speed (negative drifts the other way)
        let offset: CGFloat       // phase offset so ribbons never align
        let glow: Bool
    }

    private static let ribbons: [Ribbon] = [
        Ribbon(color: Brand.marker.blended(withFraction: 0.62, of: .black) ?? Brand.marker,
               width: 0.7, amp: 0.95, cycles: 2.6, cycles2: 4.1, speed: -0.7, offset: 2.1, glow: false),
        Ribbon(color: Brand.marker.blended(withFraction: 0.35, of: .black) ?? Brand.marker,
               width: 1.1, amp: 0.85, cycles: 2.1, cycles2: 3.3, speed: 1.25, offset: 4.4, glow: false),
        Ribbon(color: Brand.marker.blended(withFraction: 0.25, of: .white) ?? Brand.marker,
               width: 1.7, amp: 0.75, cycles: 1.6, cycles2: 2.7, speed: 0.9, offset: 0, glow: true),
    ]

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    /// Feed the latest frequency-band magnitudes (0…1). The bands are split into
    /// three groups — highs drive the back ribbon, mids the middle, lows the
    /// bright front one (the voice fundamental, usually the strongest signal).
    func setBands(_ values: [Float]) {
        guard !values.isEmpty else { return }
        let third = max(values.count / 3, 1)
        func group(_ slice: ArraySlice<Float>) -> CGFloat {
            guard !slice.isEmpty else { return 0 }
            let mean = slice.reduce(0, +) / Float(slice.count)
            let peak = slice.max() ?? 0
            // Blend mean and peak: mean alone is sluggish, peak alone is jittery.
            return CGFloat(min(max(0.6 * mean + 0.4 * peak, 0), 1))
        }
        targets[2] = group(values[0..<third])                          // lows → front
        targets[1] = group(values[third..<min(2 * third, values.count)])
        targets[0] = group(values[min(2 * third, values.count)...])    // highs → back
        active = true
        if timer == nil { startAnimating() }
    }

    /// Reflect the server's voice-activity state in the meter's brightness.
    func setHearing(_ on: Bool) {
        hearingTarget = on ? 1 : 0
        // With no tick running there is nothing to ease the value, so snap it.
        if timer == nil {
            hearing = hearingTarget
            applyPresence()
        }
    }

    private func applyPresence() {
        alphaValue = Self.dimFloor + (1 - Self.dimFloor) * hearing
    }

    /// Flatten the waves and stop animating — used when a session ends.
    func reset() {
        timer?.invalidate()
        timer = nil
        active = false
        levels = [0, 0, 0]
        targets = [0, 0, 0]
        // Start the next session muted: brightness is earned by the server
        // actually reporting speech, not assumed.
        hearing = 0
        hearingTarget = 0
        applyPresence()
        needsDisplay = true
    }

    private func startAnimating() {
        let t = Timer(timeInterval: 1.0 / 60.0, repeats: true) { [weak self] _ in self?.tick() }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    private func tick() {
        phase += 0.09
        for i in 0..<levels.count {
            let a: CGFloat = targets[i] > levels[i] ? 0.45 : 0.12   // fast attack, slow decay
            levels[i] += (targets[i] - levels[i]) * a
        }
        // Ease the brightness rather than snapping it: VAD toggles on word
        // boundaries, and a hard cut would strobe the pill.
        hearing += (hearingTarget - hearing) * 0.18
        applyPresence()
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard active, let ctx = NSGraphicsContext.current?.cgContext else { return }
        let w = bounds.width
        let midY = bounds.midY
        let halfH = bounds.height / 2 - 1
        let step: CGFloat = 1.5
        let fadeSpan: CGFloat = 0.18   // fraction of the width over which the ends dissolve

        for (i, ribbon) in Self.ribbons.enumerated() {
            // A faint ripple stays even in silence so the HUD reads as listening.
            let level = max(levels[i], 0.06)
            let amp = ribbon.amp * level * halfH

            // Sample the wave, tagging each point with its end-fade factor.
            var points: [(p: CGPoint, fade: CGFloat)] = []
            var x: CGFloat = 0
            while x <= w {
                let t = x / w
                // Taper toward both ends so the ribbons converge, as in a ribbon of light.
                let envelope = pow(sin(.pi * t), 1.3)
                let s1 = sin(t * 2 * .pi * ribbon.cycles + phase * ribbon.speed + ribbon.offset)
                let s2 = sin(t * 2 * .pi * ribbon.cycles2 - phase * ribbon.speed * 1.4 + ribbon.offset * 1.7)
                let y = midY + amp * envelope * (0.62 * s1 + 0.38 * s2)
                let edge = min(max(min(t, 1 - t) / fadeSpan, 0), 1)
                points.append((CGPoint(x: x, y: y), edge * edge * (3 - 2 * edge)))
                x += step
            }

            // Build the ribbon as one filled variable-width shape: offset each
            // sample along the curve normal by half the local width, walk the
            // top edge out and the bottom edge back. The width tapers with the
            // end fade, so the tips converge to a point and dissolve smoothly
            // instead of stopping dead at the component edge — with no seams,
            // unlike stroking in per-segment passes.
            let n = points.count
            var top = [CGPoint](); top.reserveCapacity(n)
            var bottom = [CGPoint](); bottom.reserveCapacity(n)
            for j in 0..<n {
                let prev = points[max(j - 1, 0)].p
                let next = points[min(j + 1, n - 1)].p
                let dx = next.x - prev.x, dy = next.y - prev.y
                let len = max(sqrt(dx * dx + dy * dy), 0.001)
                let halfW = max(ribbon.width * points[j].fade, 0.05) / 2
                let nx = -dy / len * halfW, ny = dx / len * halfW
                top.append(CGPoint(x: points[j].p.x + nx, y: points[j].p.y + ny))
                bottom.append(CGPoint(x: points[j].p.x - nx, y: points[j].p.y - ny))
            }
            let shape = CGMutablePath()
            shape.addLines(between: top)
            shape.addLines(between: bottom.reversed())
            shape.closeSubpath()

            ctx.saveGState()
            if ribbon.glow {
                ctx.setShadow(offset: .zero, blur: 4,
                              color: Brand.marker.withAlphaComponent(0.9).cgColor)
            }
            ctx.setFillColor(ribbon.color.cgColor)
            ctx.addPath(shape)
            ctx.fillPath()
            ctx.restoreGState()
        }
    }
}
