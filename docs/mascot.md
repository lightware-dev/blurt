# Blurt mascot — "Blip"

The Blurt mascot is **Blip**: the blinking text cursor (`▍`) from the logo, grown a
face, arms, and feet. This document is the source of truth for who Blip is, how it's
drawn, and the design decisions behind it.

> Status: concept locked (character + personality + state set). Not yet wired into
> `www/`. Next step under discussion: animate the impatience→panic beats as an inline-SVG
> hero.

---

## 1. The one-line

**Blip is a text cursor with a caffeine problem.** It sits at the end of your line
blinking, *bored out of its mind*, waiting for you to say something — and the instant
you actually blurt, it can't type fast enough and scrambles to keep up.

That tension **is** the character, and it's on-brand: the site already jokes that Blurt
"types what you meant, not what you mumbled." Blip is that joke drawn as a face — eager,
impatient, a little overwhelmed, honest about the letters it dropped.

---

## 2. Why a cursor bar (concept history)

Four concepts were sketched before settling. All in the same palette, all rendered on
the dot-grid.

| # | Name | What it was | Verdict |
|---|------|-------------|---------|
| **A** | **Blip** — the cursor bar | The `▍` logo mark with a face, caught mid-blurt | **Chosen.** Introduces *zero* new brand vocabulary — it literally is the favicon, so it collapses back to the 32px mark already shipped. |
| B | Marky — the highlighter | A walking chisel-tip marker laying down clean text | Strong "we highlighted your mess" tie-in, but a second object to maintain. |
| C | blurtd — the daemon | A friendly terminal imp: cursor-bar horns, caret tail, glowing GPU belly | Great for the "Meet blurtd" section; too niche as *the* mascot. |
| D | The Blurt — speech blob | A speech bubble with a face; mumble in, clean text out | Purest onomatopoeia, weakest execution. |

**Decision:** A (Blip). It reuses the existing logo mark, so mascot and favicon are the
same object at different sizes — no brand drift. C ("blurtd" daemon) is kept in the back
pocket as a possible companion/section illustration.

---

## 3. Personality & the canonical arc

Blip's personality only fully reads as a **sequence**. The canonical 5-beat arc (this is
the definition of the character, per the brief "waiting a lot, impatient, but then
struggles to keep up when you blurt"):

| # | State | Pose | Inner monologue (caption) |
|---|-------|------|---------------------------|
| 01 | **waiting** | heavy-lidded, hand on hip, foot tapping | `any day now.` |
| 02 | **still waiting** | checking a little coral wristwatch, one brow up | `…anytime.` |
| 03 | **oh!** | REC light pops, arms fly up, sparkle eyes | `oh! finally—` |
| 04 | **can't keep up** | leaning back from the firehose, **ghost/blur arms** typing frantically, words streaking in, panic eyes, sweat, mouth wide | `wait— WAIT—` |
| 05 | **wrecked** | X-eyes, tongue out, a couple of dropped letters on the floor | `…got most of it.` |

Beat **04** is the money shot and the recommended hero image. Beat **05**'s caption —
"…got most of it." — is the most on-brand line: self-deprecating, honest, exactly the
voice of the site.

### Reduced state set (for real UI, e.g. the menu-bar app)
When the full comic isn't appropriate, collapse to the product lifecycle. These map onto
the actual pipeline (`⌥Space` → VAD → Parakeet → paste):

- **idle** — impatient/bored, blinking waiting-caret beside it
- **listening** — REC dot on, alert, one hand cupped
- **thinking** — glancing up, rising `…` dots (partial decode)
- **blurting** — determined, open mouth, sound waves (live transcription)
- **done** — happy closed eyes, marker check `✓` (text delivered)

---

## 4. Brand palette & type (must match `www/src/app/global.css`)

Blip is drawn **only** in the site tokens. No new colors.

| Token | Hex | Role in Blip |
|-------|-----|--------------|
| `--color-ink-950` | `#090a0c` | Face features (eyes, mouth, brows); card/background |
| `--color-ink-900` | `#0f1115` | State-card background |
| `--color-ink-700` | `#23262d` | Card borders; dot-grid dots |
| `--color-bone` | `#ece6d8` | Panic eye-whites; waiting caret; drifting letters |
| `--color-bone-dim` | `#8f8b7e` | Motion streaks, quieter sound waves, captions |
| `--color-marker` | `#e8ff32` | **Blip's body** — highlighter yellow, the whole point |
| `--color-coral` | `#ff5943` | REC dot, tongue, wristwatch, sweat, alarm accents |

- **Body fill:** `marker` (`#e8ff32`), flat — matches the site's flat aesthetic. No
  gradients.
- **Features:** `ink-950` (`#090a0c`).
- **Fonts:** captions/labels use **JetBrains Mono** (`--font-mono`); the site's display
  face is **Space Grotesk**, body is **Inter**.
- **Selection/hero surfaces** sit on the `.dotgrid` (radial `ink-700` dots, 22px grid).

---

## 5. Construction spec

Blip is deliberately simple so it scales from favicon to hero and is quick to re-pose.

- **Body:** a single rounded-rect "cursor bar." In a 240-wide tile: `x=80 y=70 w=80
  h=140 rx=24`. Aspect ≈ 0.57 (w/h) — clearly a caret, not a blob.
- **Feet:** two `22×12 rx=6` marker rounded-rects at the base, ~40px apart. Raise one for
  the tapping-foot gag.
- **Arms:** stubby `stroke-width≈14`, `stroke-linecap=round` marker strokes off the
  body's mid-sides. Everything expressive happens in arm curves + face.
- **Eyes:** two `ink` circles (r≈8–13 by emotion), centered ~`cx 104 / 136`, `cy≈120`,
  each with a small `marker` catch-light dot. Vary for state:
  - bored → marker "eyelid" rect covering the top third + a lash line
  - alert/panic → bigger circles, `bone` whites, tiny high pupils
  - happy/done → upward `^ ^` arc strokes
  - wrecked → crossed `X` strokes
- **Mouth:** flat curve (bored) → small `o` (listening) → `ink` ellipse + `coral` tongue
  (blurting/panic).
- **Motion vocabulary:** ghost limbs = the same arm path repeated at `opacity≈0.25`;
  sweat = small coral/bone teardrops; incoming speech = `bone-dim` streak lines + small
  marker/bone "word" rounded-rects rushing in from the right; thought/partial = rising
  marker dots.
- **Shadow:** one soft `#000 @ opacity 0.4` ellipse under the feet.

### Favicon relationship
The shipped favicon (`www/public/favicon.svg`) is a `#e8ff32` bar on a `#090a0c`
rounded square. Blip **is** that bar with a face — at 16–32px, drop the face and you're
back to the favicon. Keep them in sync.

---

## 6. Assets

Reference renders and SVG source were produced during design. The canonical SVG source
is embedded in the Appendix so this file is self-contained and reproducible.

- **Base state set** (idle · listening · thinking · blurting · done) → `expressions.svg`
- **Character arc** (waiting → wrecked, the personality) → `arc.svg`

Rendering (headless Chrome, transparent bg, 2×):

```
google-chrome --headless --disable-gpu --force-device-scale-factor=2 \
  --screenshot=out.png --window-size=1300,400 \
  --default-background-color=00000000 arc.svg
```

---

## 7. Open threads / next steps

1. **Animate the arc** (recommended). The site already ships `@keyframes blink` and
   `sweep` in `global.css`; beat 01's foot-tap + caret-blink and beat 04's arm-blur loop
   as a lightweight **inline-SVG** hero — no video, no JS.
2. **Hero composition** — use beat 04 (the scramble) big next to the "We made one anyway."
   headline, or run all five beats as a strip.
3. **Menu-bar states** — wire the reduced set (idle/listening/blurting/done) into the Mac
   client.
4. **Keep `blurtd` (concept C)** as a possible companion illustration for the "Meet
   blurtd" section.

---

## Appendix A — base state set (`expressions.svg`)

```svg
<!-- 240×340 per tile · idle · listening · thinking · blurting · done -->
<!-- Body per tile: rect x=80 y=70 w=80 h=140 rx=24 fill #e8ff32 -->
<!-- Features #090a0c · catch-lights/thought-dots/check #e8ff32 · REC/tongue #ff5943 -->
<!-- panic whites #ece6d8 · motion/captions #8f8b7e · cards #0f1115 stroke #23262d -->
```

*(Full SVG source lives beside this doc as `expressions.svg`; regenerate renders with the
command in §6. Key face recipes are specified in §5.)*

## Appendix B — character arc (`arc.svg`)

```svg
<!-- 260×400 per tile · 01 waiting · 02 still-waiting · 03 oh! · 04 can't-keep-up · 05 wrecked -->
<!-- Beat 04 (hero): body rotate(-7); ghost arms = arm path @ opacity .25; -->
<!--   incoming words = #8f8b7e streak lines + marker/bone rounded-rects rushing in; -->
<!--   panic eyes = #ece6d8 whites + tiny high #090a0c pupils; coral sweat + coral tongue. -->
<!-- Beat 05: X-eyes (crossed strokes), coral tongue, two dropped letters (rotated rects). -->
<!-- Captions: JetBrains Mono; blurting/panic captions in #ff5943, "oh! finally" in #e8ff32. -->
```

*(Full SVG source lives beside this doc as `arc.svg`.)*
