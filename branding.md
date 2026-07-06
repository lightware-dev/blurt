# Blurt — brand & positioning

> **Say it badly, get it typed well.**

Local, GPU-accelerated voice dictation: press a hotkey, blurt out whatever's in
your head, and clean text lands in the field you're already focused on. Runs
entirely on your own hardware (Parakeet on the box, native menu-bar app on the
Mac) — your voice never leaves the LAN.

## The angle: playful, honest, self-aware

Every competitor in this space is named some earnest variant of **Whisper**
(Superwhisper, Wispr Flow, OpenWhispr, Whispur, Overwhisper, Whisperstream,
WhisperTyping…) or leans on the parrot pun (OpenQuack). **Blurt stands out by
*not* taking itself seriously.**

The name owns a real tension and turns it into the whole joke: **the chaos is on
your end; the output is clean.** You blurt — half-formed, rambling, no
punctuation — and the tool *behaves*, handing back composed text. That honesty
(*"say it badly"*) is more disarming and more memorable than one more promise of
"effortless, 99% accurate AI voice."

**Personality:** fast, personal power-tool with a wink. Confident enough to joke
about how messy human speech is. Not enterprise, not precious.

**Where it fights you:** "Blurt" reads playful, not corporate. If this ever needs
to be sold into a law firm or a compliance-driven buyer, the name works against
you. For a local, personal, developer-flavored tool with character, that's a
feature — it's the reason it's memorable in a sea of earnest clones.

## One-liners / taglines

Primary:

- **Say it badly, get it typed well.**

Alternates:

- Think out loud. We'll fix the grammar.
- For people who talk faster than they type.
- Press ⌥Space and just… let it out.
- Your voice, your GPU, your text. Nothing leaves the room.  ← privacy-forward variant

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

## Name & slug availability

Checked 2026-07-06. "Voice space" = any existing dictation/STT product.

| Slug | Status | Notes |
|---|---|---|
| **Brand "Blurt"** | ✅ clear in voice space | no dictation/STT product uses it |
| `github.com/blurtvoice` (org) | ✅ OWNED | grabbed; repo lives at `blurtvoice/blurt` |
| `github.com/blurt` (org) | ⚠️ taken | unrelated; we use the `blurtvoice` org instead |
| npm `blurt` | ⚠️ taken | dead fire-and-forget messaging lib, last publish 2017 — unrelated |
| npm **`blurtd`** | ✅ FREE | ideal for the server/daemon package |
| PyPI `blurt` / `blurtd` | ✅ FREE | both open |
| **`blurtvoice.com`** | ✅ REGISTERED | **primary domain (owned)** |
| `blurtvoice.app` | ✅ FREE | good secondary |
| `blurtdictation.com` | ✅ FREE | descriptive fallback |
| `blurt.app` | ⚠️ taken | |
| `blurt.io` / `blurt.sh` | ⚠️ taken | |
| `getblurt.com` / `blurtapp.com` | ⚠️ taken | |

**Recommended handle set:**

- **Brand:** Blurt · **Mac app:** Blurt.app · bundle id `app.blurtvoice.menubar`
- **Repo:** `blurtvoice/blurt` (GitHub org `blurtvoice` is owned)
- **Server package (if ever published):** `blurtd` (npm + PyPI both free) —
  "the blurt daemon" is also just a funny sentence
- **Domain:** **blurtvoice.com** (primary, registered), blurtvoice.app /
  blurtdictation.com (available as secondary)

### For reference — the shortlist Blurt beat

| Name | Verdict |
|---|---|
| **Trill** | strong runner-up; bird/Parakeet nod, `trillvoice.app` free |
| Loqui | polished but abstract; premium domains taken |
| Parley | softer fit ("negotiation"); domains tight |
| Sotto | ❌ npm `sotto` is a *live* "voice input for Claude Code" (2026) |
| Utter | ❌ homophone of Otter.ai; handles taken |

## Website (`www/`)

The original brief that produced the marketing site — keep it as the north star
for future edits:

- **Stack:** Next.js (App Router) + Tailwind, deployed to Cloudflare. Mirror
  `../chronicle/www` exactly (Next 16 + Tailwind v4 + OpenNext → Cloudflare
  Workers; `opennextjs-cloudflare build && deploy`). Worker name `blurt`; target
  domain `blurtvoice.com`.
- **Design brief:** fun and witty. **Draw on how crowded the space already is —
  lean into "yet another" dictation app.** Creative, unusual, deliberately *not*
  another AI-generated slop page that looks like every other site.
- **How that was executed** (the current site): a "brutalist dictation HUD" —
  near-black ink, warm bone text, one loud highlighter-yellow accent, display +
  mono type mix (Space Grotesk / JetBrains Mono / Inter). Centerpiece is
  `components/BlurtDemo.tsx`: a live typewriter that blurts a messy, disfluent
  sentence then snaps it to clean text — the "say it badly, get it typed well"
  promise made visual. Struck-through Whisper-clone list + naming wink own the
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
