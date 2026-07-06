"""
Registry of selectable Parakeet ASR variants.

Pick one at startup with `--model/-m <alias>` (or the PARAKEET_MODEL env, or a
full HuggingFace id). All are NeMo-loadable via ASRModel.from_pretrained and run
in bf16 by default. VRAM figures are approximate peaks in bf16 for short
utterances on this box.
"""

# alias -> metadata. `id` is the HF/NeMo model name passed to from_pretrained.
MODELS: dict[str, dict] = {
    "v3": {
        "id": "nvidia/parakeet-tdt-0.6b-v3",
        "lang": "multilingual (25 EU langs, incl. en/pt/es/fr/de)",
        "params": "0.6B",
        "vram": "~1.5 GB",
        "note": "default — best multilingual accuracy/VRAM balance",
    },
    "v2": {
        "id": "nvidia/parakeet-tdt-0.6b-v2",
        "lang": "English",
        "params": "0.6B",
        "vram": "~1.5 GB",
        "note": "best-in-class English WER (~6%)",
    },
    "v1": {
        "id": "nvidia/parakeet-tdt-0.6b",
        "lang": "English",
        "params": "0.6B",
        "vram": "~1.5 GB",
        "note": "original 0.6B TDT",
    },
    "1.1b": {
        "id": "nvidia/parakeet-tdt-1.1b",
        "lang": "English",
        "params": "1.1B",
        "vram": "~2.5 GB",
        "note": "larger English model, marginal accuracy gain, more VRAM",
    },
    "rnnt-1.1b": {
        "id": "nvidia/parakeet-rnnt-1.1b",
        "lang": "English",
        "params": "1.1B",
        "vram": "~2.5 GB",
        "note": "RNNT decoder variant",
    },
    "ctc-110m": {
        "id": "nvidia/parakeet-tdt_ctc-110m",
        "lang": "English",
        "params": "0.11B",
        "vram": "~0.5 GB",
        "note": "tiny/fastest, lowest VRAM, higher WER",
    },
}

DEFAULT_ALIAS = "v3"


def resolve(name: str | None) -> str:
    """Map an alias to a full model id; pass through anything that looks like an id."""
    if not name:
        return MODELS[DEFAULT_ALIAS]["id"]
    if name in MODELS:
        return MODELS[name]["id"]
    return name  # assume it's already a full HF/NeMo id (e.g. nvidia/...)


def describe() -> str:
    lines = ["Available Parakeet models (use with -m <alias>):", ""]
    for alias, m in MODELS.items():
        star = "  *" if alias == DEFAULT_ALIAS else "   "
        lines.append(f"{star} {alias:<10} {m['id']}")
        lines.append(f"       {m['params']:<6} {m['vram']:<9} {m['lang']} — {m['note']}")
    lines.append("")
    lines.append("You may also pass any full HuggingFace id directly, e.g. -m nvidia/parakeet-ctc-0.6b")
    return "\n".join(lines)
