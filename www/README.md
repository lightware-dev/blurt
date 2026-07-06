# blurt — www

Marketing site for Blurt. Next.js (App Router) + Tailwind v4, deployed to
Cloudflare Workers via OpenNext. Same stack as `../../chronicle/www`.

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
The centerpiece is `components/BlurtDemo.tsx` — a live typewriter that blurts a
messy, disfluent sentence and then snaps it to clean composed text, which *is*
the product's whole promise ("say it badly, get it typed well").

The tone leans into how crowded the space is ("yet another dictation app") rather
than pretending to be revolutionary. Copy voice + rationale live in the repo-root
`branding.md`.

## TODO before launch

- The `GITHUB` constant in `src/app/page.tsx` points at
  `github.com/blurtvoice/blurt` — confirm the repo is pushed there.
- Add an `og-image.png` (1200×630) for social cards.
