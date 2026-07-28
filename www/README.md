# blurt — www

Marketing site for Blurt. Next.js (App Router) + Tailwind v4, deployed to
Cloudflare Workers via OpenNext.

## Develop

```bash
npm install
npm run dev            # http://localhost:3000
```

## Deploy (Cloudflare)

```bash
npm run preview        # build + local Workers runtime preview
npm run deploy         # build + push to Cloudflare (wrangler)
```

`wrangler.toml` sets the worker name to `blurt`. Point the `blurtvoice.com`
domain at the worker in the Cloudflare dashboard (Workers ▸ Custom Domains) or
via a route once deployed.

## Design notes

The look is a deliberate anti-slop "brutalist dictation HUD": near-black ink,
warm bone text, one loud highlighter-yellow accent, and a display/mono type mix.
The centerpiece is `components/BlurtDemo.tsx` — a live typewriter that streams a
sentence into the HUD as partials and then pastes that exact transcript at the
cursor, mirroring the app: what's in the HUD is what gets typed, no rewrite step.
The pitch is speed and privacy ("talk faster than you type"), not cleanup.

The tone leans into how crowded the space is ("yet another dictation app") rather
than pretending to be revolutionary. Copy voice + rationale live in
`../docs/branding.md`.
