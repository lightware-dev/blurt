"""blurtd — the Blurt dictation daemon. Entrypoint: `./blurtd` or `python -m server`.

Serves one ASR engine — parakeet-tdt-0.6b-v3 (bf16, GPU) by default, Whisper
with --engine whisper; wss:// if certs exist.

Examples:
    ./blurtd
    ./blurtd --port 8000
    ./blurtd --engine whisper
    ./blurtd --no-preload
"""

import os
import argparse
from pathlib import Path

import uvicorn

from server import app as app_module
from server.engine import ENGINES, create_asr, resolve_engine


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blurtd", description="blurtd — the Blurt dictation daemon")
    p.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    # Default 25878 spells "BLURT" on a phone keypad (B-L-U-R-T → 2-5-8-7-8).
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "25878")))
    # Optional second listener: the Wyoming protocol (Home Assistant STT), off
    # by default. It has no auth and no TLS, so enabling it opens a path to the
    # model that AUTH_TOKEN does not cover. 10300 is the ecosystem's convention.
    p.add_argument("--wyoming-port", type=int,
                   default=int(os.getenv("WYOMING_PORT", "0")),
                   help="enable the Wyoming (Home Assistant STT) listener on this "
                        "port, e.g. 10300; 0 (default) disables it")
    # Which model this process serves. One engine per process by design — see
    # server/engine.py — so this is startup-only, not a per-request choice.
    p.add_argument("--engine", default=os.getenv("BLURT_ASR_ENGINE", ""),
                   help=f"ASR engine to serve ({'|'.join(ENGINES)}; "
                        "default: parakeet, or BLURT_ASR_ENGINE)")
    p.add_argument("--no-preload", action="store_true", help="defer model load until first dictation")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    # server.app instantiates an engine from the environment at import; a --engine
    # flag replaces it here, before anything has loaded a model. Every consumer
    # reads server.app.asr at call time, so rebinding it is enough.
    asr = app_module.asr
    try:
        wanted = resolve_engine(args.engine)
    except ValueError as e:
        parser.error(str(e))   # a typo'd engine name is a usage error, not a traceback
    if wanted != asr.engine:
        asr = app_module.asr = create_asr(wanted)

    # The Wyoming listener starts inside the app's lifespan; pass the CLI values
    # through the module globals it reads at startup.
    app_module.WYOMING_PORT = args.wyoming_port
    app_module.HOST = args.host
    if not os.getenv("WYOMING_HOST"):
        app_module.WYOMING_HOST = args.host

    if not args.no_preload:
        asr.load()  # pay the load once, up front

    # certs/ next to the source tree by default. BLURT_CERT_DIR moves it, which
    # is what the container uses: it runs unprivileged and /app is read-only to
    # it, so the auto-generated pair has to live on the cache volume instead.
    cert_dir = Path(os.getenv("BLURT_CERT_DIR") or (app_module.ROOT / "certs"))
    cert = cert_dir / "cert.pem"
    key = cert_dir / "key.pem"
    kwargs = {"host": args.host, "port": args.port, "log_level": "info"}
    if cert.exists() and key.exists():
        kwargs["ssl_certfile"] = str(cert)
        kwargs["ssl_keyfile"] = str(key)
        print(f"[blurtd] engine={asr.engine} model={asr.model_name}", flush=True)
        print(f"[blurtd] WSS on wss://<ip>:{args.port}/ws", flush=True)
    else:
        print(f"[blurtd] engine={asr.engine} model={asr.model_name}", flush=True)
        print(f"[blurtd] WS on ws://localhost:{args.port}/ws  (no certs → LAN mic blocked in browsers)", flush=True)
    # (the Wyoming listener announces itself from the app lifespan once it's bound)
    uvicorn.run(app_module.app, **kwargs)


if __name__ == "__main__":
    main()
