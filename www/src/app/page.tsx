import BlurtDemo from './components/BlurtDemo'

const GITHUB = 'https://github.com/blurtvoice/blurt'

// The real, crowded field. We are, technically, competing with all of these.
const RIVALS = [
    'Whisper',
    'Wispr',
    'Superwhisper',
    'OpenWhispr',
    'Whispur',
    'Overwhisper',
    'Whisperstream',
    'WhisperTyping',
    'Weesper',
    'Willow',
    'OpenQuack',
]

// Names we seriously considered before picking the honest one.
const REJECTED = ['Trill', 'Loqui', 'Parley', 'Sotto', 'Utter']

const STATS = [
    { big: '~80ms', small: 'to transcribe a 35-second clip' },
    { big: '1.4 GB', small: 'VRAM. your GPU won’t notice.' },
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
                    one that runs on <span className="mark font-medium">your</span> GPU and types
                    what you <em className="not-italic text-bone">meant</em>, not what you{' '}
                    <em className="not-italic text-bone">mumbled</em>.
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

                {/* the star of the show */}
                <div className="mt-14">
                    <BlurtDemo />
                    <p className="mt-3 text-center font-mono text-xs text-bone-dim">
                        press <KeyCap>⌥</KeyCap>
                        <KeyCap>Space</KeyCap>, ramble, release. clean text where your cursor is.
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
                        <KeyCap>Space</KeyCap>. Talk. Done.
                    </h2>
                    <p className="mt-4 max-w-xl text-bone/70">
                        No “workspace”. No onboarding flow. The entire product is one hotkey
                        and a pipeline that fits in a diagram:
                    </p>

                    <div className="mt-10 flex flex-col gap-3 font-mono text-sm sm:flex-row sm:items-stretch sm:gap-0">
                        {[
                            ['🎙️', 'mic', '16 kHz PCM16, straight off AVAudioEngine'],
                            ['✂️', 'VAD', 'Silero splits your speech at the pauses'],
                            ['⚡', 'Parakeet', 'GPU re-decodes every ~350ms, low WER'],
                            ['⌨️', 'your cursor', 'final text pasted into the focused field'],
                        ].map(([icon, title, sub], idx, arr) => (
                            <div key={title} className="flex items-stretch sm:flex-1">
                                <div className="flex-1 rounded-xl border border-ink-700 bg-ink-950 p-4">
                                    <div className="text-lg">{icon}</div>
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
                    Say it badly.
                    <br />
                    Get it typed <span className="mark">well</span>.
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
                    <span className="text-bone">⌥Space</span>. that’s the whole funnel.
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
                    </div>
                    <nav className="flex flex-wrap gap-x-6 gap-y-1 font-mono text-xs text-bone-dim">
                        <a href={GITHUB} className="hover:text-bone">
                            github
                        </a>
                        <a href={`${GITHUB}#server-linux--gpu`} className="hover:text-bone">
                            server
                        </a>
                        <a href={`${GITHUB}/tree/main/client-mac`} className="hover:text-bone">
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
