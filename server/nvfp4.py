"""NVFP4 snapshot format — a pre-quantized 4-bit parakeet-tdt-0.6b-v3.

Quantizing at startup is self-defeating: the GPU has to hold the full bf16 model
before it can be compressed, so the 4-bit weights save nothing on a card that
couldn't fit bf16 in the first place. A snapshot moves quantization offline —
calibrate once with scripts/build_nvfp4_snapshot.py, then ship packed weights
that load straight onto the device.

NVFP4 is E2M1: four bits per weight (sign, 2 exponent, 1 mantissa — eight
representable magnitudes), grouped in blocks of 16, each block carrying its own
E4M3 fp8 scale. The block scale is what makes four bits survivable, because
precision is spent locally: a block of small weights is not crushed by a large
outlier elsewhere in the row. Only the 217 encoder Linears are quantized (86% of
the weights); the convolutions, LSTM decoder and joint network stay bf16, and
activations stay bf16 too (W4A16).

A snapshot directory holds:
  model_config.yaml    NeMo config, tokenizer paths rewritten to local names
  <tokenizer files>    SentencePiece model and vocabs, from the source .nemo
  recipe.json          quantizer layout and calibrated amax values
  weights.safetensors  packed uint8 weights (two 4-bit values per byte), fp8
                       block scales, and the bf16 remainder

Nothing here is a pickle. `torch.load` reconstructs arbitrary Python objects, so
loading one is equivalent to running its author's code — a poor property for a
file the daemon downloads at startup. Weights use safetensors (a length-prefixed
JSON header over raw tensor bytes, structurally unable to carry code), the recipe
is JSON, the config YAML.

Measured against the bf16 checkpoint on an RTX 5090, 208 clips: same WER within
noise (2.86% vs 2.99%, 95% CI [-0.69, +0.30] pp), 0.78 GB of VRAM against 1.43,
and 2.4x the decode latency — this trades speed for memory, and is worth choosing
only when memory is what you are short of.
"""

from __future__ import annotations

import contextlib
import importlib
import json
import os

# Files inside a snapshot directory.
WEIGHTS = "weights.safetensors"
RECIPE = "recipe.json"
CONFIG = "model_config.yaml"
MANIFEST = "snapshot.json"

# Tokenizer config keys holding file paths. Everything else in that block (`type`,
# `hf_kwargs`) must be left alone — rewriting `type: bpe` into a path makes NeMo
# reject the tokenizer with a misleading "must be `bpe` or `wpe`".
TOKENIZER_PATH_KEYS = ("dir", "model_path", "vocab_path", "spe_tokenizer_vocab")

# Tags for values JSON cannot represent losslessly. Both matter for NVFP4: the
# layout is `num_bits: (2, 1)`, a tuple, and `block_sizes: {-1: 16}`, which has an
# integer key. Plain JSON returns a list and a string key, and modelopt's config
# validator rejects each — so they are tagged on the way out and restored on the
# way in rather than silently degraded.
DTYPE_TAG = "__torch_dtype__"
CLASS_TAG = "__class__"
TUPLE_TAG = "__tuple__"
ITEMS_TAG = "__items__"

# The recipe names one class (modelopt's NVFP4 tensor type). It is stored as an
# import path, which stays safe only because it is allowlisted and resolved by
# attribute lookup — never called. That is precisely the guarantee a pickle
# cannot make, where any callable in any installed package can be named and
# invoked during load.
ALLOWED_CLASS_PREFIXES = ("modelopt.",)


def to_jsonable(obj):
    """Rewrite a structure so JSON carries it without losing type information."""
    if isinstance(obj, dict):
        if all(isinstance(k, str) for k in obj):
            return {k: to_jsonable(v) for k, v in obj.items()}
        return {ITEMS_TAG: [[to_jsonable(k), to_jsonable(v)] for k, v in obj.items()]}
    if isinstance(obj, tuple):
        return {TUPLE_TAG: [to_jsonable(v) for v in obj]}
    if isinstance(obj, list):
        return [to_jsonable(v) for v in obj]
    return obj


def write_recipe(state: dict, path: str) -> None:
    """Serialize modelopt's quantization state as JSON.

    Anything without an explicit encoding is left to raise rather than being
    coerced by a `default=` fallback: a recipe that quietly dropped a field would
    still load, and would then compute the wrong thing with no symptom.
    """
    import torch

    payload = to_jsonable({
        "modelopt_version": state["modelopt_version"],
        "modes": [[mode, blob] for mode, blob in state["modelopt_state_dict"]],
    })

    def encode(obj):
        if isinstance(obj, torch.dtype):
            return {DTYPE_TAG: str(obj).removeprefix("torch.")}
        if isinstance(obj, type):
            ref = f"{obj.__module__}.{obj.__qualname__}"
            if not ref.startswith(ALLOWED_CLASS_PREFIXES):
                raise TypeError(f"recipe references class {ref}, outside the "
                                f"allowlist {ALLOWED_CLASS_PREFIXES}")
            return {CLASS_TAG: ref}
        raise TypeError(f"snapshot recipe holds a {type(obj).__name__}, which has "
                        f"no pickle-free encoding: {obj!r}")

    with open(path, "w") as fh:
        json.dump(payload, fh, allow_nan=False, default=encode)


def read_recipe(path: str) -> dict:
    """Rebuild the structure modelopt expects from the JSON form."""
    import torch

    def decode(d):
        if len(d) == 1 and DTYPE_TAG in d:
            return getattr(torch, d[DTYPE_TAG])
        if len(d) == 1 and TUPLE_TAG in d:
            return tuple(d[TUPLE_TAG])
        if len(d) == 1 and ITEMS_TAG in d:
            # Keys arrive already decoded: object_hook runs innermost-first.
            return {(tuple(k) if isinstance(k, list) else k): v for k, v in d[ITEMS_TAG]}
        if len(d) == 1 and CLASS_TAG in d:
            ref = d[CLASS_TAG]
            if not ref.startswith(ALLOWED_CLASS_PREFIXES):
                raise ValueError(f"snapshot names class {ref} outside the allowlist "
                                 f"{ALLOWED_CLASS_PREFIXES}; refusing to import it")
            mod, _, name = ref.rpartition(".")
            return getattr(importlib.import_module(mod), name)
        return d

    with open(path) as fh:
        payload = json.load(fh, object_hook=decode)
    return {
        "modelopt_version": payload["modelopt_version"],
        "modelopt_state_dict": [(mode, blob) for mode, blob in payload["modes"]],
    }


def model_class(cfg):
    """Resolve the NeMo model class the config names."""
    mod, _, cls = cfg["target"].rpartition(".")
    return getattr(importlib.import_module(mod), cls)


@contextlib.contextmanager
def _cheap_init():
    """Zero-fill instead of randomly initializing the throwaway skeleton.

    Every parameter is replaced by the snapshot's own tensors (load_snapshot
    checks none is missing), so the seconds NeMo spends drawing kaiming/normal
    values for 627M parameters buy nothing. Zeros rather than uninitialized
    memory because the recipe replay compresses these weights before they are
    overwritten, and garbage could hold NaN.
    """
    import torch
    import torch.nn.init as init

    def zero(tensor, *args, **kwargs):
        with torch.no_grad():
            return tensor.zero_()

    saved = {n: getattr(init, n) for n in dir(init)
             if n.endswith("_") and not n.startswith("_") and callable(getattr(init, n))}
    for name in saved:
        setattr(init, name, zero)
    try:
        yield
    finally:
        for name, fn in saved.items():
            setattr(init, name, fn)


def load_snapshot(snapshot_dir: str, device: str = "cuda"):
    """Load a snapshot with no bf16 weights ever reaching the device.

    Three steps: build the module skeleton on the CPU from config (the 1.25 GB
    bf16 checkpoint is never read and the GPU is untouched), replay the modelopt
    recipe so every quantized Linear takes its packed uint8 form, then read the
    packed tensors from safetensors straight onto the device and bind them in.

    Steps one and two are CPU overhead that should not have to exist — a module
    graph is built and compressed only to be overwritten — and they dominate the
    ~21 s load. They remain because NeMo cannot construct this model on the meta
    device (ConformerEncoder.__init__ calls .item()) and modelopt's restore
    replays compression rather than installing the packed layout directly.
    Neither touches the GPU, which is what the memory saving depends on.
    """
    import torch
    from omegaconf import OmegaConf, open_dict
    from safetensors.torch import load_file
    import modelopt.torch.opt as mto

    for required in (CONFIG, RECIPE, WEIGHTS):
        path = os.path.join(snapshot_dir, required)
        if not os.path.exists(path):
            raise RuntimeError(f"snapshot at {snapshot_dir} is missing {required}")

    cfg = OmegaConf.load(os.path.join(snapshot_dir, CONFIG))
    with open_dict(cfg):   # tokenizer artifacts are stored beside the config
        for key in TOKENIZER_PATH_KEYS:
            val = cfg.get("tokenizer", {}).get(key)
            if isinstance(val, str) and val and not os.path.isabs(val):
                cfg.tokenizer[key] = os.path.join(snapshot_dir, val)

    torch.set_default_dtype(torch.bfloat16)
    try:
        with _cheap_init():
            model = model_class(cfg)(cfg=cfg)
    finally:
        torch.set_default_dtype(torch.float32)
    model.eval()

    mto.restore_from_modelopt_state(model, read_recipe(os.path.join(snapshot_dir, RECIPE)))

    state = load_file(os.path.join(snapshot_dir, WEIGHTS), device=device)
    missing, unexpected = model.load_state_dict(state, strict=False, assign=True)
    if missing:
        raise RuntimeError(
            f"snapshot at {snapshot_dir} is missing {len(missing)} tensors the model "
            f"needs (first: {list(missing)[:3]}). Those parameters would keep the "
            "skeleton's zeros, so the model would load and transcribe nonsense.")
    if unexpected:
        print(f"[asr] warn: snapshot has {len(unexpected)} unused tensors "
              f"(first: {list(unexpected)[:3]})", flush=True)
    model.to(device)

    # Not every mel-featurizer buffer is in the state_dict, so the skeleton's
    # float32 copies survive and the first encoder conv sees fp32 features against
    # bf16 weights. Convert the preprocessor alone: a blanket model.to(bfloat16)
    # would also rewrite the fp8 block scales and destroy the quantized weights.
    model.preprocessor.to(torch.bfloat16)
    model.eval()
    return model
