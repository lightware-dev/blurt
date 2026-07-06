'use client'

import { useEffect, useState } from 'react'

/**
 * The whole pitch, animated: you blurt something half-formed and rambling (mono,
 * dimmed, with the "uh"s), the tool thinks for a beat, and clean composed text
 * lands where your cursor is. "Say it badly, get it typed well."
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

export default function BlurtDemo() {
    const [i, setI] = useState(0)
    const [typed, setTyped] = useState('')
    const [phase, setPhase] = useState<'raw' | 'thinking' | 'clean'>('raw')

    useEffect(() => {
        let cancelled = false
        const pair = PAIRS[i]

        async function run() {
            setPhase('raw')
            setTyped('')
            // type the messy blurt, char by char, with human-ish jitter
            for (let c = 1; c <= pair.raw.length; c++) {
                if (cancelled) return
                setTyped(pair.raw.slice(0, c))
                const ch = pair.raw[c - 1]
                await sleep(ch === ' ' ? 26 : 30 + Math.random() * 45)
            }
            await sleep(520)
            if (cancelled) return

            setPhase('thinking')
            await sleep(650)
            if (cancelled) return

            setPhase('clean')
            await sleep(3000)
            if (cancelled) return

            setI(prev => (prev + 1) % PAIRS.length)
        }

        run()
        return () => {
            cancelled = true
        }
    }, [i])

    return (
        <div className="mx-auto w-full max-w-2xl overflow-hidden rounded-2xl border border-ink-700 bg-ink-900 shadow-2xl shadow-black/40">
            {/* HUD title bar */}
            <div className="flex items-center gap-3 border-b border-ink-700 bg-ink-850 px-4 py-3">
                <span
                    className="inline-block size-2.5 rounded-full bg-coral"
                    style={{ boxShadow: '0 0 10px var(--color-coral)' }}
                />
                <span className="font-mono text-xs tracking-wide text-bone-dim">
                    {phase === 'clean' ? 'typed into your editor' : 'Blurting…'}
                </span>
                <span className="ml-auto flex items-center gap-1 font-mono text-[11px] text-bone-dim">
                    <kbd className="rounded border border-ink-600 bg-ink-800 px-1.5 py-0.5">⌥</kbd>
                    <kbd className="rounded border border-ink-600 bg-ink-800 px-1.5 py-0.5">Space</kbd>
                </span>
            </div>

            {/* body */}
            <div className="min-h-[168px] px-5 py-6 sm:min-h-[152px] sm:px-7">
                {phase !== 'clean' ? (
                    <p className="font-mono text-[15px] leading-relaxed text-bone/55 sm:text-base">
                        {typed}
                        <span className="ml-0.5 inline-block w-[0.55em] animate-blink bg-bone/70 align-baseline">
                            {' '}
                        </span>
                        {phase === 'thinking' && (
                            <span className="ml-2 text-bone-dim">cleaning up…</span>
                        )}
                    </p>
                ) : (
                    <p
                        key={i}
                        className="font-sans text-lg leading-relaxed text-bone sm:text-xl"
                    >
                        <span className="mark-sweep">{PAIRS[i].clean}</span>
                    </p>
                )}
            </div>
        </div>
    )
}
