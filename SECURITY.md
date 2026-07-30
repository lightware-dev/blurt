# Security policy

## Reporting a vulnerability

**Please do not open a public issue for a security problem.**

Report it privately through GitHub's [private vulnerability
reporting](https://github.com/lightware-dev/blurt/security/advisories/new) —
Security ▸ Report a vulnerability on the repository. That opens a draft advisory
visible only to you and the maintainers.

Please include:

- what an attacker can do, and what access they need to start (same LAN? the
  same machine? a user already dictating?);
- the affected component — `server/`, `clients/mac/`, `clients/windows/`, or the
  protocol itself;
- steps to reproduce, ideally against a stock `./blurtd`;
- the version or commit you tested.

We aim to acknowledge a report within a week and to agree a disclosure timeline
with you from there. Blurt is maintained by a small team, so please allow
reasonable time for a fix before publishing. We're happy to credit you in the
advisory and the release notes unless you'd rather stay anonymous.

## Supported versions

Blurt is developed on `main` and fixes land there first. Only the latest release
receives security updates; there are no long-term support branches.

## Scope

In scope: the `server/` daemon and its three network surfaces (the native
WebSocket protocol, the OpenAI-compatible `/v1` API, and the Wyoming listener),
the macOS and Windows clients, and the release/update mechanisms.

Out of scope: the marketing site under `www/`, and vulnerabilities in upstream
dependencies (NeMo, PyTorch, Silero VAD, FastAPI, Next.js) — report those to the
projects themselves, though do tell us if Blurt's usage makes an upstream issue
meaningfully worse.

## Known and intended limitations

These are deliberate design choices, documented so they don't get reported as
vulnerabilities. If you can show that one is worse than described here, that
*is* worth reporting.

- **The server is unauthenticated by default.** `AUTH_TOKEN` is empty unless you
  set it, and the daemon binds `0.0.0.0`. Blurt is built for a trusted LAN, on
  the assumption that anyone who can reach the port is allowed to dictate. Set
  `AUTH_TOKEN` and firewall the port if that isn't true of your network.
- **TLS is self-signed.** `scripts/gen_certs.sh` produces a certificate no
  authority vouches for. The native clients pin it instead: you confirm its
  SHA-256 fingerprint once, and from then on they refuse anything else for that
  `host:port` and warn loudly if it changes. That authenticates the server
  against a machine-in-the-middle *after* the first connection — the first one is
  trust-on-first-use, and a browser pointed at the mic page still just shows the
  usual "not private" interstitial. A certificate that validates against the
  system trust store is also accepted without a pin, so an attacker holding a
  publicly-trusted certificate for your server's hostname is still in scope for
  the LAN threat model above.
- **The Wyoming listener has no auth and no TLS**, matching the Wyoming
  ecosystem's norm, and `AUTH_TOKEN` does not apply to it. It is therefore
  **disabled by default** and must be enabled explicitly with a port.
- **Clients inject text into whatever field has focus.** That is the product.
  Transcribed text is typed or pasted without inspection, so a server you don't
  control can put arbitrary text into your active application — point clients
  only at servers you trust.

## What Blurt does with your audio

Audio is streamed to the server you configured and transcribed there. Nothing is
sent anywhere else, and no audio or transcript is written to disk by the server.
With `LOG_STATS` enabled the server logs per-dictation metadata only — packet
counts, byte counts, durations — never transcribed text.
