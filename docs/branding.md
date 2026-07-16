# Blurt — brand & positioning

> **Talk faster than you type.**

Local, GPU-accelerated voice dictation: press a hotkey, blurt out whatever's in
your head, and the transcript lands in the field you're already focused on —
fast enough to finish before you do. Runs entirely on your own hardware
(Parakeet on the box, native menu-bar app on the Mac) — your voice never leaves
the LAN.

## The angle: playful, honest, self-aware

Every competitor in this space is named some earnest variant of **Whisper**
(Superwhisper, Wispr Flow, OpenWhispr, Whispur, Overwhisper, Whisperstream,
WhisperTyping…) or leans on the parrot pun (OpenQuack). **Blurt stands out by
*not* taking itself seriously.**

The name owns a real tension and turns it into the whole joke: **you talk faster
than you type, so stop typing.** You blurt it out — messy, fast, mid-thought —
and the tool keeps up, dropping your words into the field before you'd have
finished reaching for the keyboard. Owning that it's *"yet another"* dictation
app is more disarming and more memorable than one more promise of "effortless,
99% accurate AI voice." (Blurt transcribes what you said — it doesn't rewrite or
clean up your grammar. That's the honest version.)

**Personality:** fast, personal power-tool with a wink. Confident enough to joke
about how messy human speech is. Not enterprise, not precious.

**Where it fights you:** "Blurt" reads playful, not corporate. If this ever needs
to be sold into a law firm or a compliance-driven buyer, the name works against
you. For a local, personal, developer-flavored tool with character, that's a
feature — it's the reason it's memorable in a sea of earnest clones.

## One-liners / taglines

Primary:

- **Talk faster than you type.**

Alternates:

- For people who talk faster than they type.
- Think out loud. It's already typed.
- Press ⌥Space and just… let it out.
- Your voice, your GPU, your text. Nothing leaves the room.  ← privacy-forward variant

## `blurtd` — the daemon (and the best joke we have)

The server/daemon is named **`blurtd`**, and the whole gag is that it's the past
tense of "blurted" — a background process on your GPU whose identity is "blurted."
Launch it with `./blurtd` (thin wrapper over `python -m server`). Rich comedic
material worth reusing anywhere:

- **`systemctl status blurtd`** → `Active: blurting (running)`. The site has a
  fake-terminal section built on this.
- "There is no `blurtd stop`. You press ⌥Space again. That's the stop."
- "A daemon named 'blurted.' We're at peace with it."
- Log prefix is `[blurtd]`; argparse `prog` is `blurtd`.
- Package name `blurtd` is free on npm + PyPI if ever published.

## UI voice & copy notes

Keep the playful register in the *chrome* (menu, HUD, empty states) — never at the
expense of the text output, which stays clean and literal.

Applied in-app (shipped):

| Surface | Copy |
|---|---|
| Menu — toggle item | `Start / Stop Blurting  (⌥Space)` |
| Menu — quit | `Quit Blurt` |
| HUD while recording | `Blurting…` |
| Status-bar glyph a11y label | `Blurt` |
| Server / FastAPI title | `Blurt — Parakeet dictation server` |
| Browser test page | `🗣️ Blurt — mic test` |

Ideas for later (not yet wired — need small behavior/UI additions):

- **Empty final transcript:** `Nothing blurted.`
- **Auto-punctuation toggle:** `Make me sound composed`
- **HUD partials label:** the live, unfiltered text = the *"brain dump."*
- **First-run tip:** *"Press ⌥Space and just start talking. Don't overthink it —
  that's the point."*

## Visual / mascot (optional, cheap to skip)

A speech bubble caught **mid-blurt**: an open bubble with a `//` text cursor
blinking inside it. Reads as "words coming out, being turned into text," and
reduces cleanly to a monochrome menu-bar glyph. Current placeholder uses the
system `mic` / `mic.fill` SF Symbol (red when recording).

## Names & identifiers

- **Brand:** Blurt · **Mac app:** Blurt.app · bundle id `com.blurtvoice.mac`
  (platform-named tail leaves room for `com.blurtvoice.ios` etc.)
- **Repo:** `lightware-dev/blurt`
- **Daemon / server:** `blurtd` — "the blurt daemon" is also just a funny sentence
- **Site:** blurtvoice.com

## Website (`www/`)

The original brief that produced the marketing site — keep it as the north star
for future edits:

- **Stack:** Next.js (App Router, Next 16) + Tailwind v4, built with OpenNext and
  deployed to Cloudflare Workers (`opennextjs-cloudflare build && deploy`). Worker
  name `blurt`; target domain `blurtvoice.com`.
- **Design brief:** fun and witty. **Draw on how crowded the space already is —
  lean into "yet another" dictation app.** Creative, unusual, deliberately *not*
  another AI-generated slop page that looks like every other site.
- **How that was executed** (the current site): a "brutalist dictation HUD" —
  near-black ink, warm bone text, one loud highlighter-yellow accent, display +
  mono type mix (Space Grotesk / JetBrains Mono / Inter). Centerpiece is
  `components/BlurtDemo.tsx`: a live typewriter that streams a sentence into the
  HUD as partials, then pastes that exact transcript at the cursor — what's in
  the HUD is what gets typed, no rewrite step, the "talk faster than you type"
  pitch made visual. Struck-through Whisper-clone list + naming wink own the
  crowded-field angle. Tone is self-aware, anti-corporate ("not a startup, not
  hiring, not raising").

### Running / deploying the site

```bash
cd www
npm install
npm run dev -- -H 0.0.0.0 -p 3300   # remote-reachable dev (localhost:3000 is taken)
npm run preview                      # Cloudflare Workers runtime preview
npm run deploy                       # build + wrangler deploy
```

Note: on `localhost`, the `preview`/`deploy` step needs the `workerd` + `esbuild`
postinstall binaries, which the box's `allow-scripts` guard blocks on a plain
`npm install` — approve those scripts before the Workers runtime will run.
