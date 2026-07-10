'use client'

import { useEffect, useRef, useState } from 'react'

/**
 * The whole pitch, animated: you blurt something half-formed and rambling into
 * the HUD (the same near-black pill the Mac app shows — flowing yellow waveform,
 * live mono partials truncating from the left), release the hotkey, and clean
 * composed text lands where your cursor is. "Say it badly, get it typed well."
 */
const PAIRS: { raw: string; clean: string }[] = [
    {
        raw: 'um so like, can you send maria the uh the q3 deck before before standup',
        clean: 'Can you send Maria the Q3 deck before standup?',
    },
    {
        raw: 'note to self buy oat milk and uh cancel that subscription i keep forgetting about',
        clean: 'Note to self: buy oat milk and cancel that subscription I keep forgetting about.',
    },
    {
        raw: 'hey team just shipping a quick fix for the login thing that was that was breaking on safari',
        clean: 'Hey team — shipping a quick fix for the login bug that was breaking on Safari.',
    },
    {
        raw: 'reply to the client and tell them yeah friday works but not not before noon',
        clean: 'Reply to the client: yes to Friday, but not before noon.',
    },
]

const sleep = (ms: number) => new Promise(r => setTimeout(r, ms))

// ── the HUD waveform, ported from the Mac app (clients/mac/…/Waveform.swift):
// three sine ribbons drift horizontally and swell with the voice. The front
// ribbon is the brightest — marker yellow with a soft glow — and the ones behind
// are progressively darker and thinner, so intensity reads as brightness. On the
// site there is no live FFT, so a small speech-envelope generator stands in for
// the mic: bursts of "voice" with pauses, fast attack, slow decay.
const RIBBONS = [
    // marker #e8ff32 blended 62% toward black — the back ribbon
    { color: '#586113', width: 0.7, amp: 0.95, cycles: 2.6, cycles2: 4.1, speed: -0.7, offset: 2.1, glow: false },
    // blended 35% toward black — the middle ribbon
    { color: '#97a621', width: 1.1, amp: 0.85, cycles: 2.1, cycles2: 3.3, speed: 1.25, offset: 4.4, glow: false },
    // blended 25% toward white — the bright front ribbon
    { color: '#eeff65', width: 1.7, amp: 0.75, cycles: 1.6, cycles2: 2.7, speed: 0.9, offset: 0, glow: true },
]

const WAVE_W = 96
const WAVE_H = 34

function Waveform({ speaking }: { speaking: boolean }) {
    const canvasRef = useRef<HTMLCanvasElement | null>(null)
    const speakingRef = useRef(speaking)
    speakingRef.current = speaking

    useEffect(() => {
        const canvas = canvasRef.current
        if (!canvas) return
        const ctx = canvas.getContext('2d')
        if (!ctx) return
        const dpr = window.devicePixelRatio || 1
        canvas.width = WAVE_W * dpr
        canvas.height = WAVE_H * dpr
        ctx.scale(dpr, dpr)

        let phase = 0
        let frame = 0
        let raf = 0
        const levels = [0, 0, 0]
        const targets = [0, 0, 0]

        const midY = WAVE_H / 2
        const halfH = WAVE_H / 2 - 1
        const step = 1.5
        const fadeSpan = 0.18 // fraction of the width over which the ends dissolve

        const tick = () => {
            // stand-in for the live FFT bands: lows drive the bright front
            // ribbon, mids/highs the dimmer ones behind it
            if (frame % 5 === 0) {
                if (speakingRef.current) {
                    const talking = Math.random() < 0.85
                    const v = talking ? 0.3 + Math.random() * 0.7 : 0.04 + Math.random() * 0.08
                    targets[2] = v
                    targets[1] = v * (0.45 + Math.random() * 0.45)
                    targets[0] = v * (0.3 + Math.random() * 0.45)
                } else {
                    for (let i = 0; i < 3; i++) targets[i] = 0.02 + Math.random() * 0.06
                }
            }
            frame++
            phase += 0.09
            for (let i = 0; i < 3; i++) {
                const a = targets[i] > levels[i] ? 0.45 : 0.12 // fast attack, slow decay
                levels[i] += (targets[i] - levels[i]) * a
            }

            ctx.clearRect(0, 0, WAVE_W, WAVE_H)
            RIBBONS.forEach((ribbon, i) => {
                // a faint ripple stays even in silence so the HUD reads as listening
                const level = Math.max(levels[i], 0.06)
                const amp = ribbon.amp * level * halfH

                // sample the wave, tagging each point with its end-fade factor
                const pts: { x: number; y: number; fade: number }[] = []
                for (let x = 0; x <= WAVE_W; x += step) {
                    const t = x / WAVE_W
                    // taper toward both ends so the ribbons converge to points
                    const envelope = Math.sin(Math.PI * t) ** 1.3
                    const s1 = Math.sin(t * 2 * Math.PI * ribbon.cycles + phase * ribbon.speed + ribbon.offset)
                    const s2 = Math.sin(t * 2 * Math.PI * ribbon.cycles2 - phase * ribbon.speed * 1.4 + ribbon.offset * 1.7)
                    const y = midY + amp * envelope * (0.62 * s1 + 0.38 * s2)
                    const edge = Math.min(Math.max(Math.min(t, 1 - t) / fadeSpan, 0), 1)
                    pts.push({ x, y, fade: edge * edge * (3 - 2 * edge) })
                }

                // one filled variable-width shape per ribbon: offset each sample
                // along the curve normal, walk the top edge out and the bottom
                // edge back; the width tapers with the end fade so the tips
                // dissolve instead of stopping dead
                const n = pts.length
                const top: [number, number][] = []
                const bottom: [number, number][] = []
                for (let j = 0; j < n; j++) {
                    const prev = pts[Math.max(j - 1, 0)]
                    const next = pts[Math.min(j + 1, n - 1)]
                    const dx = next.x - prev.x
                    const dy = next.y - prev.y
                    const len = Math.max(Math.hypot(dx, dy), 0.001)
                    const halfW = Math.max(ribbon.width * pts[j].fade, 0.05) / 2
                    const nx = (-dy / len) * halfW
                    const ny = (dx / len) * halfW
                    top.push([pts[j].x + nx, pts[j].y + ny])
                    bottom.push([pts[j].x - nx, pts[j].y - ny])
                }

                ctx.save()
                if (ribbon.glow) {
                    ctx.shadowBlur = 4
                    ctx.shadowColor = 'rgba(232, 255, 50, 0.9)'
                }
                ctx.fillStyle = ribbon.color
                ctx.beginPath()
                top.forEach(([x, y], j) => (j === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)))
                for (let j = n - 1; j >= 0; j--) ctx.lineTo(bottom[j][0], bottom[j][1])
                ctx.closePath()
                ctx.fill()
                ctx.restore()
            })

            raf = requestAnimationFrame(tick)
        }

        raf = requestAnimationFrame(tick)
        return () => cancelAnimationFrame(raf)
    }, [])

    return <canvas ref={canvasRef} aria-hidden className="h-[34px] w-[96px] shrink-0" />
}

type Phase = 'listening' | 'raw' | 'processing' | 'clean'

export default function BlurtDemo() {
    const [i, setI] = useState(0)
    const [typed, setTyped] = useState('')
    const [phase, setPhase] = useState<Phase>('listening')
    const [clipped, setClipped] = useState(false)
    const clipRef = useRef<HTMLSpanElement | null>(null)

    useEffect(() => {
        let cancelled = false
        const pair = PAIRS[i]

        async function run() {
            // hotkey down: the HUD pops up and listens for a beat
            setPhase('listening')
            setTyped('')
            await sleep(700)
            if (cancelled) return

            // live partials stream into the HUD, char by char with human-ish jitter
            setPhase('raw')
            for (let c = 1; c <= pair.raw.length; c++) {
                if (cancelled) return
                setTyped(pair.raw.slice(0, c))
                const ch = pair.raw[c - 1]
                await sleep(ch === ' ' ? 26 : 30 + Math.random() * 45)
            }
            await sleep(420)
            if (cancelled) return

            // hotkey released: the HUD just vanishes — no "cleaning up…" status,
            // same as the app — and a beat later the clean text lands at the cursor
            setPhase('processing')
            await sleep(450)
            if (cancelled) return

            setPhase('clean')
            await sleep(3200)
            if (cancelled) return
            setI(prev => (prev + 1) % PAIRS.length)
        }

        run()
        return () => {
            cancelled = true
        }
    }, [i])

    // Head truncation, like the app's byTruncatingHead label: once the partials
    // outgrow the pill, the right end stays put and old words slide off the left
    // under a fade (the web stand-in for the "…").
    useEffect(() => {
        const el = clipRef.current
        if (!el || !el.parentElement) return
        setClipped(el.scrollWidth > el.parentElement.clientWidth + 1)
    }, [typed, phase])

    const hudVisible = phase === 'listening' || phase === 'raw'

    return (
        <div className="mx-auto w-full max-w-2xl overflow-hidden rounded-2xl border border-ink-700 bg-ink-950/80 shadow-2xl shadow-black/40 backdrop-blur-sm">
            {/* where your cursor is — any app with a text field */}
            <div className="flex items-center gap-2 border-b border-ink-800 bg-ink-900/60 px-4 py-2.5">
                <span className="size-3 rounded-full bg-coral/80" />
                <span className="size-3 rounded-full bg-marker/70" />
                <span className="size-3 rounded-full bg-bone/25" />
                <span className="ml-2 font-mono text-xs text-bone-dim">
                    wherever your cursor is
                </span>
                <span className="ml-auto flex items-center gap-1 font-mono text-[11px] text-bone-dim">
                    <kbd className="rounded border border-ink-600 bg-ink-800 px-1.5 py-0.5">⌥</kbd>
                    <kbd className="rounded border border-ink-600 bg-ink-800 px-1.5 py-0.5">Space</kbd>
                </span>
            </div>

            {/* the editor line the clean text is pasted into */}
            <div className="min-h-[92px] px-6 pt-6 sm:px-8">
                {phase === 'clean' ? (
                    <p key={i} className="font-sans text-lg leading-relaxed text-bone sm:text-xl">
                        <span className="mark-sweep">{PAIRS[i].clean}</span>
                        <span className="ml-1 inline-block w-[0.55em] animate-blink bg-bone/70 align-baseline">
                            {' '}
                        </span>
                    </p>
                ) : (
                    <p className="font-sans text-lg leading-relaxed sm:text-xl">
                        <span className="inline-block w-[0.55em] animate-blink bg-bone/70 align-baseline">
                            {' '}
                        </span>
                    </p>
                )}
            </div>

            {/* the HUD — the Mac app's pill, verbatim (clients/mac/…/HUD.swift):
                560×76, ink-900 at 85%, radius 18, ink-700 hairline, waveform on
                the left, one line of live mono partials truncating from the head */}
            <div className="flex justify-center px-4 pb-6 pt-4">
                <div
                    className={`flex h-[76px] w-full max-w-[560px] items-center rounded-[18px] border border-ink-700 bg-ink-900/85 shadow-xl shadow-black/50 transition-all duration-200 ${
                        hudVisible ? 'opacity-100' : 'translate-y-1 opacity-0'
                    }`}
                >
                    <div className="ml-[18px] h-[34px] w-[96px] shrink-0">
                        {hudVisible && <Waveform speaking={phase === 'raw'} />}
                    </div>
                    <div
                        className={`ml-3 mr-[22px] flex flex-1 justify-end overflow-hidden ${
                            clipped
                                ? '[mask-image:linear-gradient(to_right,transparent,black_32px)]'
                                : ''
                        }`}
                    >
                        <span
                            ref={clipRef}
                            className={`grow whitespace-nowrap text-left font-mono ${
                                phase === 'listening'
                                    ? 'text-[17px] text-bone-dim'
                                    : 'text-[18px] font-medium text-bone'
                            }`}
                        >
                            {phase === 'listening' ? 'Listening…' : typed}
                        </span>
                    </div>
                </div>
            </div>
        </div>
    )
}
