"""blurtd — the Blurt dictation daemon. Entrypoint: `./blurtd` or `python -m server`.

Serves parakeet-tdt-0.6b-v3 (bf16, GPU); wss:// if certs exist.

Examples:
    ./blurtd
    ./blurtd --port 8000
    ./blurtd --no-preload
"""

import os
import argparse

import uvicorn

from server import app as app_module


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="blurtd", description="blurtd — the Blurt (Parakeet) dictation daemon")
    p.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    # Default 25878 spells "BLURT" on a phone keypad (B-L-U-R-T → 2-5-8-7-8).
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "25878")))
    p.add_argument("--no-preload", action="store_true", help="defer model load until first dictation")
    return p


def main():
    args = build_parser().parse_args()

    asr = app_module.asr  # the singleton instantiated in server.app

    if not args.no_preload:
        asr.load()  # pay the load once, up front

    cert = app_module.ROOT / "certs" / "cert.pem"
    key = app_module.ROOT / "certs" / "key.pem"
    kwargs = {"host": args.host, "port": args.port, "log_level": "info"}
    if cert.exists() and key.exists():
        kwargs["ssl_certfile"] = str(cert)
        kwargs["ssl_keyfile"] = str(key)
        print(f"[blurtd] model={asr.model_name}", flush=True)
        print(f"[blurtd] WSS on wss://<ip>:{args.port}/ws", flush=True)
    else:
        print(f"[blurtd] model={asr.model_name}", flush=True)
        print(f"[blurtd] WS on ws://localhost:{args.port}/ws  (no certs → LAN mic blocked in browsers)", flush=True)
    uvicorn.run(app_module.app, **kwargs)


if __name__ == "__main__":
    main()
