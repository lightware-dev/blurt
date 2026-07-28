import BlurtDemo from './components/BlurtDemo'

const GITHUB = 'https://github.com/lightware-dev/blurt'

// The real, crowded field. We are, technically, competing with all of these.
const RIVALS = [
    'Whisper',
    'Wispr Flow',
    'Superwhisper',
    'Openwhispr',
    'Whispur',
    'Overwhisper',
    'Whisperstream',
    'Whispering',
    'WhisperTyping',
    'Weesper',
    'Willow',
    'OpenQuack',
]

// Names we seriously considered before picking the honest one.
const REJECTED = ['Trill', 'Loqui', 'Parley', 'Sotto', 'Utter']

const STATS = [
    { big: '~70ms', small: 'to transcribe a 35-second clip' },
    { big: '~2.3 GB', small: 'VRAM. your GPU won’t notice.' },
    { big: '0 bytes', small: 'leave your network' },
    { big: '$0/mo', small: 'forever. it’s just code.' },
    { big: '25', small: 'languages (incl. 🇬🇧 + 🇵🇹)' },
]

function KeyCap({ children }: { children: React.ReactNode }) {
    return (
        <kbd className="mx-0.5 inline-block rounded-md border border-ink-600 bg-ink-800 px-2 py-0.5 font-mono text-[0.85em] text-bone shadow-[0_2px_0_var(--color-ink-700)]">
            {children}
        </kbd>
    )
}

// Single-colour, outline pipeline icons (lucide-style, stroke on currentColor).
const PIPE_ICONS: Record<string, React.ReactNode> = {
    mic: (
        <>
            <path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
            <line x1="12" x2="12" y1="19" y2="22" />
        </>
    ),
    scissors: (
        <>
            <circle cx="6" cy="6" r="3" />
            <path d="M8.12 8.12 12 12" />
            <path d="M20 4 8.12 15.88" />
            <circle cx="6" cy="18" r="3" />
            <path d="M14.8 14.8 20 20" />
        </>
    ),
    zap: (
        <path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z" />
    ),
    cursor: (
        <>
            <path d="M17 22h-1a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4h1" />
            <path d="M7 22h1a4 4 0 0 0 4-4V6a4 4 0 0 0-4-4H7" />
        </>
    ),
}

function PipeIcon({ name }: { name: string }) {
    return (
        <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
            className="h-8 w-8 text-marker"
        >
            {PIPE_ICONS[name]}
        </svg>
    )
}

export default function Home() {
    return (
        <main className="dotgrid min-h-screen">
            {/* ── nav ─────────────────────────────────────────────── */}
            <header className="mx-auto flex max-w-5xl items-center justify-between px-6 py-6">
                <div className="flex items-center gap-2 font-display text-xl font-bold tracking-tight">
                    <span className="animate-blink text-marker">▍</span>
                    <span>Blurt</span>
                </div>
                <a
                    href={GITHUB}
                    className="font-mono text-sm text-bone-dim underline-offset-4 hover:text-bone hover:underline"
                >
                    /github
                </a>
            </header>

            {/* ── hero ────────────────────────────────────────────── */}
            <section className="mx-auto max-w-5xl px-6 pt-10 pb-20 sm:pt-16">
                <p className="mb-6 inline-flex items-center gap-2 rounded-full border border-ink-700 bg-ink-900/70 px-3 py-1 font-mono text-xs text-bone-dim">
                    <span className="size-1.5 rounded-full bg-marker" />
                    yet another dictation app™
                </p>

                <h1 className="font-display text-5xl font-bold leading-[0.98] tracking-tight sm:text-7xl">
                    The world did not need
                    <br />
                    another voice-to-text app.
                </h1>

                <p className="mt-6 max-w-2xl font-display text-2xl font-medium text-marker sm:text-3xl">
                    We made one anyway.
                </p>

                <p className="mt-6 max-w-xl text-lg leading-relaxed text-bone/80">
                    The category is <em className="not-italic text-bone">aggressively</em> crowded — it’s
                    mostly the word “Whisper” with the vowels moved around. Blurt is the
                    one that runs on <span className="mark font-medium">your</span> GPU, never
                    phones home, and types what you said before you’re{' '}
                    <em className="not-italic text-bone">done saying it</em>.
                </p>

                <div className="mt-9 flex flex-wrap items-center gap-4">
                    <a
                        href={GITHUB}
                        className="rounded-xl bg-marker px-6 py-3 font-display font-bold text-ink-950 transition hover:brightness-95"
                    >
                        ★ Steal it on GitHub
                    </a>
                    <a
                        href="#why"
                        className="rounded-xl border border-ink-600 px-6 py-3 font-mono text-sm text-bone transition hover:bg-ink-900"
                    >
                        read the honest pitch ↓
                    </a>
                </div>

                <p className="mt-4 font-mono text-xs text-bone-dim">
                    no account · no cloud · no “we’ve updated our privacy policy” email
                </p>

                <p className="mt-4 font-mono text-xs text-bone-dim">
                    got a server running? grab the Mac app:{' '}
                    <code className="text-bone">brew install --cask lightware-dev/tap/blurt</code>
                </p>

                {/* the star of the show */}
                <div className="mt-14">
                    <BlurtDemo />
                    <p className="mt-3 text-center font-mono text-xs text-bone-dim">
                        tap <KeyCap>⌥</KeyCap>
                        <KeyCap>⌥</KeyCap>, ramble, tap again to finish (<KeyCap>Esc</KeyCap> to
                        cancel). final text lands where your cursor is.
                    </p>
                </div>
            </section>

            {/* ── the crowded field ───────────────────────────────── */}
            <section className="border-y border-ink-800 bg-ink-900/40">
                <div className="mx-auto max-w-5xl px-6 py-16">
                    <h2 className="font-mono text-sm uppercase tracking-widest text-bone-dim">
                        // names we are technically competing with
                    </h2>
                    <div className="mt-6 flex flex-wrap gap-x-6 gap-y-3 font-display text-3xl font-semibold text-bone-dim sm:text-4xl">
                        {RIVALS.map(name => (
                            <span key={name} className="relative">
                                <span className="line-through decoration-coral/70 decoration-2">
                                    {name}
                                </span>
                            </span>
                        ))}
                        <span className="mark text-3xl font-bold sm:text-4xl">Blurt</span>
                    </div>
                    <p className="mt-6 max-w-lg text-bone/70">
                        We went with the sound your mouth actually makes. Branding is hard;
                        onomatopoeia is free.
                    </p>
                </div>
            </section>

            {/* ── why bother ──────────────────────────────────────── */}
            <section id="why" className="mx-auto max-w-5xl px-6 py-20">
                <h2 className="max-w-2xl font-display text-4xl font-bold tracking-tight sm:text-5xl">
                    So why build #401?
                </h2>
                <p className="mt-4 max-w-xl text-bone/70">
                    Fair question. Three honest reasons — no “revolutionary”, no
                    “AI-powered”, no “reimagining the way you work.”
                </p>

                <div className="mt-12 grid gap-5 sm:grid-cols-3">
                    {[
                        {
                            k: '01',
                            h: 'It’s yours.',
                            p: 'Runs on your box. Your voice never touches a cloud, an API key, or someone’s training set. No account. No telemetry. Nothing to leak.',
                        },
                        {
                            k: '02',
                            h: 'It’s stupid fast.',
                            p: 'NVIDIA Parakeet on a 5090 transcribes a 35-second clip in about 80ms. Live partials appear as you talk. It finishes before you do.',
                        },
                        {
                            k: '03',
                            h: 'It’s free. Genuinely.',
                            p: 'It’s a Python server and a Swift menu-bar app. There is no pricing page, no seat, no Series A, no “Pro” tier dangling the good features.',
                        },
                    ].map(c => (
                        <div
                            key={c.k}
                            className="rounded-2xl border border-ink-700 bg-ink-900 p-6 transition hover:border-ink-600"
                        >
                            <div className="font-mono text-sm text-marker">{c.k}</div>
                            <h3 className="mt-4 font-display text-xl font-bold">{c.h}</h3>
                            <p className="mt-2 text-sm leading-relaxed text-bone/70">{c.p}</p>
                        </div>
                    ))}
                </div>
            </section>

            {/* ── how it works ────────────────────────────────────── */}
            <section className="border-y border-ink-800 bg-ink-900/40">
                <div className="mx-auto max-w-5xl px-6 py-20">
                    <h2 className="font-display text-4xl font-bold tracking-tight sm:text-5xl">
                        <KeyCap>⌥</KeyCap>
                        <KeyCap>⌥</KeyCap>. Talk. Done.
                    </h2>
                    <p className="mt-4 max-w-xl text-bone/70">
                        No “workspace”. No onboarding flow. The entire product is one hotkey
                        and a pipeline that fits in a diagram:
                    </p>

                    <div className="mt-10 flex flex-col gap-3 font-mono text-sm sm:flex-row sm:items-stretch sm:gap-0">
                        {[
                            ['mic', 'mic', '16 kHz PCM16, straight off AVAudioEngine'],
                            ['scissors', 'VAD', 'Silero splits your speech at the pauses'],
                            ['zap', 'Parakeet', 'GPU re-decodes every ~350ms, low WER'],
                            ['cursor', 'your cursor', 'final text pasted into the focused field'],
                        ].map(([icon, title, sub], idx, arr) => (
                            <div key={title} className="flex items-stretch sm:flex-1">
                                <div className="flex-1 rounded-xl border border-ink-700 bg-ink-950 p-4">
                                    <PipeIcon name={icon} />
                                    <div className="mt-2 font-display text-base font-bold text-bone">
                                        {title}
                                    </div>
                                    <div className="mt-1 text-xs leading-snug text-bone-dim">
                                        {sub}
                                    </div>
                                </div>
                                {idx < arr.length - 1 && (
                                    <div className="flex items-center px-2 text-marker sm:px-1">
                                        →
                                    </div>
                                )}
                            </div>
                        ))}
                    </div>
                    <p className="mt-6 font-mono text-xs text-bone-dim">
                        VRAM is bounded by your <span className="text-bone">longest single sentence</span>,
                        not the length of the session. It’ll happily run all day.
                    </p>
                </div>
            </section>

            {/* ── meet blurtd ─────────────────────────────────────── */}
            <section className="mx-auto max-w-5xl px-6 py-20">
                <h2 className="font-display text-4xl font-bold tracking-tight sm:text-5xl">
                    Meet <span className="font-mono text-marker">blurtd</span>.
                </h2>
                <p className="mt-4 max-w-xl text-bone/70">
                    The server half is a daemon. We named it{' '}
                    <code className="font-mono text-bone">blurtd</code> — which is, if you
                    say it out loud, the past tense of what it does. There is a background
                    process on your GPU whose entire identity is “blurted.” It’s fine. It’s
                    happy.
                </p>

                <div className="mt-8 overflow-hidden rounded-2xl border border-ink-700 bg-ink-950 font-mono text-[13px] shadow-2xl shadow-black/40 sm:text-sm">
                    <div className="flex items-center gap-2 border-b border-ink-700 bg-ink-850 px-4 py-2.5">
                        <span className="size-3 rounded-full bg-coral/80" />
                        <span className="size-3 rounded-full bg-marker/70" />
                        <span className="size-3 rounded-full bg-bone/25" />
                        <span className="ml-2 text-xs text-bone-dim">gpu-box — bash</span>
                    </div>
                    <pre className="overflow-x-auto px-5 py-5 leading-relaxed text-bone/85">
                        <span className="text-bone-dim">$ </span>
                        <span className="text-bone">systemctl status blurtd</span>
                        {'\n'}
                        <span className="text-marker">●</span> blurtd — the Blurt daemon
                        {'\n'}
                        {'   '}Loaded: loaded (
                        <span className="text-bone">/opt/blurt/blurtd.service</span>; enabled)
                        {'\n'}
                        {'   '}Active:{' '}
                        <span className="text-marker">blurting (running)</span> since you
                        double-tapped ⌥
                        {'\n'}
                        {'     '}Docs: it’s the past tense of “blurted.” we regret nothing.
                        {'\n'}
                        {' '}Main PID: 1337 (parakeet-tdt-0.6b-v3)
                        {'\n'}
                        {'   '}Listen:{' '}
                        <span className="text-bone">wss://0.0.0.0:25878/ws</span> — port
                        spells BLURT on a phone keypad
                        {'\n'}
                        {'    '}Tasks: 1 (listening)
                        {'\n'}
                        {'   '}Memory: 2.3G
                        {'\n'}
                        {'   '}Egress:{' '}
                        <span className="text-bone">
                            0 bytes — your voice never leaves this box
                        </span>
                        {'\n'}
                    </pre>
                </div>
                <p className="mt-3 font-mono text-xs text-bone-dim">
                    // port <span className="text-bone">25878</span> is not random —
                    2-5-8-7-8 spells <span className="text-bone">BLURT</span> on a phone
                    keypad (B→2, L→5, U→8, R→7, T→8). override with{' '}
                    <span className="text-bone">--port</span> or{' '}
                    <span className="text-bone">PORT</span>.
                </p>
                <p className="mt-2 font-mono text-xs text-bone-dim">
                    // no, there is no `blurtd stop`. double-tap ⌥ again to finish, or Esc to bail.
                </p>
            </section>

            {/* ── stats ───────────────────────────────────────────── */}
            <section className="mx-auto max-w-5xl px-6 py-20">
                <div className="grid grid-cols-2 gap-px overflow-hidden rounded-2xl border border-ink-700 bg-ink-700 sm:grid-cols-5">
                    {STATS.map(s => (
                        <div key={s.small} className="bg-ink-950 p-6">
                            <div className="font-display text-3xl font-bold text-marker sm:text-4xl">
                                {s.big}
                            </div>
                            <div className="mt-2 text-xs leading-snug text-bone-dim">
                                {s.small}
                            </div>
                        </div>
                    ))}
                </div>
            </section>

            {/* ── the naming wink ─────────────────────────────────── */}
            <section className="border-t border-ink-800 bg-ink-900/40">
                <div className="mx-auto max-w-5xl px-6 py-20">
                    <h2 className="font-display text-4xl font-bold tracking-tight sm:text-5xl">
                        We almost called it something classy.
                    </h2>
                    <div className="mt-8 flex flex-wrap items-center gap-x-6 gap-y-2 font-display text-2xl font-semibold text-bone-dim sm:text-3xl">
                        {REJECTED.map(n => (
                            <span key={n} className="line-through decoration-coral/60 decoration-2">
                                {n}
                            </span>
                        ))}
                        <span className="font-mono text-base text-bone-dim">→</span>
                        <span className="mark">Blurt</span>
                    </div>
                    <p className="mt-6 max-w-lg text-bone/70">
                        Classy is for products with a sales team. This one is for you and a
                        GPU you already paid for.
                    </p>
                </div>
            </section>

            {/* ── final CTA ───────────────────────────────────────── */}
            <section className="mx-auto max-w-5xl px-6 py-24 text-center">
                <h2 className="mx-auto max-w-2xl font-display text-4xl font-bold leading-tight tracking-tight sm:text-6xl">
                    Talk faster
                    <br />
                    than you <span className="mark">type</span>.
                </h2>
                <div className="mt-10 flex flex-wrap justify-center gap-4">
                    <a
                        href={GITHUB}
                        className="rounded-xl bg-marker px-7 py-3.5 font-display font-bold text-ink-950 transition hover:brightness-95"
                    >
                        ★ Clone it, run it, forget it exists
                    </a>
                </div>
                <p className="mt-5 font-mono text-xs text-bone-dim">
                    clone → <span className="text-bone">pip install</span> → build the Mac app →{' '}
                    <span className="text-bone">⌥⌥</span>. that’s the whole funnel.
                </p>
            </section>

            {/* ── footer ──────────────────────────────────────────── */}
            <footer className="border-t border-ink-800">
                <div className="mx-auto flex max-w-5xl flex-col gap-4 px-6 py-10 sm:flex-row sm:items-center sm:justify-between">
                    <div>
                        <div className="flex items-center gap-2 font-display font-bold">
                            <span className="text-marker">▍</span> Blurt
                        </div>
                        <p className="mt-1 max-w-md text-xs text-bone-dim">
                            A local voice-dictation thing. Not a startup. Not hiring. Not
                            raising. Runs on hardware you already own.
                        </p>
                        <p className="mt-2 text-xs text-bone-dim">
                            built by{' '}
                            <a
                                href="https://lightware.dev"
                                className="text-bone hover:text-marker"
                            >
                                Lightware
                            </a>
                        </p>
                    </div>
                    <nav className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs text-bone-dim">
                        <a href={GITHUB} className="hover:text-bone">
                            github
                        </a>
                        <a href={`${GITHUB}#server--blurtd-linux--gpu`} className="hover:text-bone">
                            server
                        </a>
                        <a href={`${GITHUB}/tree/main/clients/mac`} className="hover:text-bone">
                            mac client
                        </a>
                        <span>·</span>
                        <span>name chosen by committee of one</span>
                    </nav>
                </div>
            </footer>
        </main>
    )
}
