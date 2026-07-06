"""Entrypoint: `python -m server`. Preloads the chosen model, serves wss:// if certs exist.

Examples:
    python -m server                      # default model (v3, multilingual)
    python -m server -m v2                # English-only 0.6B
    python -m server -m 1.1b --port 8000  # larger English model
    python -m server --list-models
"""

import os
import argparse

import uvicorn

from server import app as app_module
from server.models import describe, resolve, DEFAULT_ALIAS


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m server", description="Parakeet dictation server")
    p.add_argument("-m", "--model", default=os.getenv("PARAKEET_MODEL"),
                   help=f"model alias (see --list-models) or full HF id [default: {DEFAULT_ALIAS}]")
    p.add_argument("--list-models", action="store_true", help="print available models and exit")
    p.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.getenv("PORT", "7860")))
    p.add_argument("--fp32", action="store_true", help="load in fp32 (default bf16, ~half VRAM)")
    p.add_argument("--no-preload", action="store_true", help="defer model load until first dictation")
    return p


def main():
    args = build_parser().parse_args()
    if args.list_models:
        print(describe())
        return

    # Reconfigure the singleton ASR engine with the chosen model before serving.
    from server.asr import ParakeetASR
    app_module.asr = ParakeetASR(model_name=resolve(args.model), fp32=args.fp32 or None)
    asr = app_module.asr

    if not args.no_preload:
        asr.load()  # pay the load once, up front

    cert = app_module.ROOT / "certs" / "cert.pem"
    key = app_module.ROOT / "certs" / "key.pem"
    kwargs = {"host": args.host, "port": args.port, "log_level": "info"}
    if cert.exists() and key.exists():
        kwargs["ssl_certfile"] = str(cert)
        kwargs["ssl_keyfile"] = str(key)
        print(f"[server] model={asr.model_name}", flush=True)
        print(f"[server] WSS on wss://<ip>:{args.port}/ws", flush=True)
    else:
        print(f"[server] model={asr.model_name}", flush=True)
        print(f"[server] WS on ws://localhost:{args.port}/ws  (no certs → LAN mic blocked in browsers)", flush=True)
    uvicorn.run(app_module.app, **kwargs)


if __name__ == "__main__":
    main()
