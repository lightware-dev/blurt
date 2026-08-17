"""
Parakeet ASR engine — a thin, fast wrapper around NVIDIA NeMo's
parakeet-tdt-0.6b-v3 model for in-memory streaming decode.

Design goals: minimal VRAM (half precision, ~1.3 GB), low WER (full-context decode of
each speech segment), and no per-call disk I/O (we feed numpy arrays straight to
the model instead of writing a temp wav every tick like a naive prototype).

We load a half-precision .nemo (see ckpt_path) directly onto the GPU in ~13 s,
never materialising an fp32 copy. The checkpoint comes from, in order:
  1. a local cache file (from a prior run or scripts/build_bf16_ckpt.py),
  2. else a direct download of a pre-built .nemo we publish on HF (see MODEL_REPO).
If neither is available, load() raises — the server does not fall back to fetching
and converting the upstream fp32 checkpoint. Pre-build a local one offline with
scripts/build_bf16_ckpt.py.

Precision: bf16 by default. `PARAKEET_DTYPE=fp16` switches to a float16
checkpoint instead, for pre-Ampere GPUs (GTX 16xx / RTX 20xx, sm_75) that have
no bf16 support at all. fp16 has the same 16 bits but spends them differently:
10 mantissa bits to bf16's 7 (finer), against a 65504 ceiling to bf16's ~3e38
(narrower). scripts/compare_precision.py measures both on real audio.

`PARAKEET_DTYPE=nvfp4` is the third option, for cards short of VRAM: 4-bit
encoder weights, halving memory (0.78 GB peak against 1.43) at no measurable
accuracy cost but ~2.4x the decode latency. It loads a pre-quantized snapshot
rather than a .nemo, because 4-bit scales come from calibration on real audio
and not from a cast — see server/nvfp4.py.
"""

from __future__ import annotations

import os
import time
import threading

import numpy as np

SAMPLE_RATE = 16000

# NeMo 2.1 still references np.sctypes, which NumPy 2.0 removed. Restore it so the
# ASR featurizer can convert int PCM to float. Harmless no-op on NumPy < 2.
if not hasattr(np, "sctypes"):
    np.sctypes = {
        "int": [np.int8, np.int16, np.int32, np.int64],
        "uint": [np.uint8, np.uint16, np.uint32, np.uint64],
        "float": [np.float16, np.float32, np.float64, np.longdouble],
        "complex": [np.complex64, np.complex128, np.clongdouble],
        "others": [bool, object, bytes, str, np.void],
    }

# The one model we support: multilingual 0.6B TDT, run in half precision on the GPU.
MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"

# Pre-built half-precision .nemo files we publish so first run downloads one directly
# instead of pulling the ~2.4 GB fp32 checkpoint and casting it (saves the one-off
# convert and the fp32 RAM transient). Both precisions live in this one repo, told
# apart by filename. Override with PARAKEET_REPO to host your own mirror.
MODEL_REPO = os.getenv("PARAKEET_REPO") or "lightware-dev/parakeet-tdt-0.6b-v3"
BF16_FILE = "parakeet-tdt-0.6b-v3-bf16.nemo"

# Per-precision knobs. bf16 is the default; fp16 exists for sm_75 cards
# (GTX 16xx / RTX 20xx) that cannot do bf16 at all; nvfp4 trades speed for VRAM.
#
# `kind` says what is on disk. The half-precision options are a single .nemo that
# NeMo restores directly. nvfp4 is a directory of pre-quantized weights (see
# server/nvfp4.py) because quantizing at load time would defeat the point: the
# GPU would have to hold the bf16 model first.
PRECISIONS = {
    "bf16": {
        "kind": "nemo",
        "file": BF16_FILE,
        "ckpt_env": "PARAKEET_BF16_CKPT",
    },
    "fp16": {
        "kind": "nemo",
        "file": "parakeet-tdt-0.6b-v3-fp16.nemo",
        "ckpt_env": "PARAKEET_FP16_CKPT",
    },
    "nvfp4": {
        "kind": "snapshot",
        "file": "parakeet-tdt-0.6b-v3-nvfp4",
        "ckpt_env": "PARAKEET_NVFP4_SNAPSHOT",
    },
}
DEFAULT_PRECISION = "bf16"

# Accepted spellings of each precision, so PARAKEET_DTYPE takes the obvious names.
_PRECISION_ALIASES = {
    "bf16": "bf16", "bfloat16": "bf16",
    "fp16": "fp16", "float16": "fp16", "half": "fp16",
    "nvfp4": "nvfp4", "fp4": "nvfp4", "int4": "nvfp4", "4bit": "nvfp4",
}


def resolve_precision(name: str | None) -> str:
    """Normalise a precision name ('float16' -> 'fp16'); None/'' means the default."""
    if not name:
        return DEFAULT_PRECISION
    key = _PRECISION_ALIASES.get(str(name).strip().lower())
    if key is None:
        raise ValueError(
            f"Unsupported PARAKEET_DTYPE {name!r}; expected one of "
            f"{sorted(set(_PRECISION_ALIASES))}.")
    return key


class ParakeetASR:
    """Loads the model once and serialises decode calls behind a lock.

    NeMo transcribe is synchronous and not thread-safe for concurrent calls on
    one model instance, so a single lock guards it. Callers should run
    `transcribe` in a worker thread (asyncio.to_thread) to keep the event loop
    free.
    """

    def __init__(self, precision: str | None = None):
        self.model_name = MODEL_ID
        # Precision is fixed for the life of the instance: the checkpoint on disk
        # already carries it, so switching would mean reloading anyway.
        self.precision = resolve_precision(precision or os.getenv("PARAKEET_DTYPE"))
        self._model = None
        self._lock = threading.Lock()
        self.dtype = None
        self._torch = None

    @property
    def is_loaded(self) -> bool:
        """True once the model is resident and decodes will not block on a load."""
        return self._model is not None

    def ckpt_path(self) -> str:
        """Where the pre-built checkpoint for this precision lives.

        A .nemo file for bf16/fp16, a snapshot directory for nvfp4. Override per
        precision with PARAKEET_BF16_CKPT / PARAKEET_FP16_CKPT /
        PARAKEET_NVFP4_SNAPSHOT.
        """
        spec = PRECISIONS[self.precision]
        return os.getenv(spec["ckpt_env"]) or os.path.expanduser(
            f"~/.cache/blurt/{spec['file']}")

    def torch_dtype(self):
        """The torch dtype activations run in.

        nvfp4 is W4A16: only the encoder's Linear *weights* are four bits, and
        everything flowing between layers stays bf16, so this reports bfloat16 —
        the weight format is not a torch dtype and never appears as one.
        """
        import torch

        return torch.float16 if self.precision == "fp16" else torch.bfloat16

    def load(self):
        if self._model is not None:
            return self._model
        import torch
        import nemo.collections.asr as nemo_asr

        # Cap CPU intra-op parallelism. Inference runs on the GPU, so ATen's CPU
        # thread pool sits idle; leaving it at the default (one thread per core)
        # just burns thread-stack memory. This mirrors OMP_NUM_THREADS from the
        # Dockerfile and also covers the bare `./blurtd` path, where that env var
        # isn't set. See the host-RAM tuning note in the Dockerfile for the rest.
        torch.set_num_threads(int(os.getenv("OMP_NUM_THREADS", "2")))

        t0 = time.time()
        if self.precision == "fp16":
            if not torch.cuda.is_available():
                raise RuntimeError(
                    "Blurt only supports parakeet-tdt-0.6b-v3 in fp16 on a CUDA GPU; "
                    "no CUDA device was found.")
        elif not (torch.cuda.is_available() and torch.cuda.is_bf16_supported()):
            # nvfp4 lands here too: its activations are bf16, so it needs the same
            # hardware support the bf16 path does.
            raise RuntimeError(
                f"Blurt only supports parakeet-tdt-0.6b-v3 in {self.precision} on a "
                "CUDA GPU; no bf16-capable CUDA device was found. Pre-Ampere cards "
                "(sm_75 and older) can run PARAKEET_DTYPE=fp16 instead.")
        dtype = self.torch_dtype()
        ckpt = self.ckpt_path()
        if not os.path.exists(ckpt):
            ckpt = self._download_ckpt(self.precision) or ""
        if not ckpt or not os.path.exists(ckpt):
            build_hint = (
                "scripts/build_nvfp4_snapshot.py" if self.precision == "nvfp4"
                else f"scripts/build_bf16_ckpt.py --dtype {self.precision}")
            raise RuntimeError(
                f"No {self.precision} checkpoint available: neither a local cache "
                f"({self.ckpt_path()}) nor a download from {MODEL_REPO} succeeded. "
                f"Check the network / HF_TOKEN, or pre-build one with {build_hint}.")

        print(f"[asr] loading {self.precision} checkpoint {ckpt} ...", flush=True)
        if PRECISIONS[self.precision]["kind"] == "snapshot":
            # Pre-quantized: the packed 4-bit tensors are read straight onto the
            # GPU, which never holds a bf16 copy (peak ~0.78 GB, against ~2.53 GB
            # if the model were quantized after loading). See server/nvfp4.py.
            from . import nvfp4

            model = nvfp4.load_snapshot(ckpt, device="cuda")
        else:
            # The half-precision checkpoint loads straight onto the GPU with no fp32
            # ever materialising. set_default_dtype makes NeMo build the params in that
            # dtype directly (peak ~1.3 GB, vs a ~2.5 GB fp32 transient if restored as
            # fp32 then cast); the following to(dtype) also converts preprocessor
            # buffers so the featurizer's output dtype matches the half-precision convs
            # (else the mel features come out fp32 and the first conv raises a dtype
            # mismatch).
            torch.set_default_dtype(dtype)
            try:
                model = nemo_asr.models.ASRModel.restore_from(ckpt, map_location="cuda")
            finally:
                torch.set_default_dtype(torch.float32)
            model.eval()
            model.to(dtype)

        # Report the dtype activations run in. Reading it off the first parameter
        # would be wrong for nvfp4, where that may be a packed uint8 weight — a
        # storage format, not the precision anything is computed in.
        self.dtype = dtype
        _disable_cuda_graph_decoder(model)
        self._model = model
        self._torch = torch
        _quiet_nemo_transcribe_warning()
        print(f"[asr] ready in {time.time()-t0:.1f}s (dtype={self.dtype})", flush=True)
        return model

    @staticmethod
    def _download_ckpt(precision: str = DEFAULT_PRECISION):
        """Best-effort fetch of a pre-built checkpoint from MODEL_REPO.

        Returns a local path — a .nemo file for the half precisions, a directory for
        an nvfp4 snapshot — or None on any failure (missing repo, no network, private
        repo without auth); load() raises if it gets None and has no local cache. Both
        are served from huggingface_hub's own cache, so later starts return them
        without re-downloading; set HF_TOKEN to access a private mirror.
        """
        spec = PRECISIONS[precision]
        repo, fname = MODEL_REPO, spec["file"]
        try:
            from huggingface_hub import hf_hub_download, snapshot_download
        except Exception:
            return None
        try:
            print(f"[asr] fetching pre-built {precision} checkpoint {repo}/{fname} ...",
                  flush=True)
            if spec["kind"] == "snapshot":
                # A snapshot is a directory, so pull just that prefix and point at
                # the subdirectory inside the returned repo root.
                root = snapshot_download(repo, allow_patterns=f"{fname}/*")
                path = os.path.join(root, fname)
                if not os.path.isdir(path):
                    raise RuntimeError(f"{repo} has no {fname}/ directory")
            else:
                path = hf_hub_download(repo, fname)
            print(f"[asr] downloaded {precision} checkpoint -> {path}", flush=True)
            return path
        except Exception as e:  # load() turns a None into a clear no-checkpoint error
            print(f"[asr] warn: could not fetch {precision} checkpoint ({e})", flush=True)
            return None

    @staticmethod
    def _save_ckpt(model, ckpt: str):
        """Best-effort save of the half-precision model so later starts load it directly."""
        try:
            os.makedirs(os.path.dirname(ckpt), exist_ok=True)
            model.save_to(ckpt)
            print(f"[asr] cached checkpoint -> {ckpt}", flush=True)
        except Exception as e:  # a cache miss is not worth failing startup over
            print(f"[asr] warn: could not cache checkpoint: {e}", flush=True)

    def transcribe(self, audio_f32: np.ndarray) -> str:
        """Decode a mono float32 16 kHz array to text. Returns '' for empty/silent input."""
        if audio_f32 is None or len(audio_f32) == 0:
            return ""
        model = self.load()
        audio = np.ascontiguousarray(audio_f32, dtype=np.float32)
        with self._lock:
            # inference_mode drops autograd bookkeeping — lower activation VRAM than
            # no_grad. NeMo 2.x accepts a list of numpy arrays directly (no temp wav).
            with self._torch.inference_mode():
                result = model.transcribe([audio], batch_size=1, verbose=False)
        return _extract_text(result)

    def release_cache(self):
        """Return cached CUDA blocks to the driver so peak VRAM doesn't stick.

        Called after a long segment is committed: a big utterance can spike
        activation memory; freeing it keeps steady-state VRAM near the model
        weights (~1.5 GB) instead of the high-water mark.
        """
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()


def _disable_cuda_graph_decoder(model):
    """Turn off NeMo's CUDA-graph TDT/RNNT greedy decoder.

    The graph decoder captures a CUDA graph on the first decode and *replays* it
    on every subsequent call, with pointers baked in to caching-allocator blocks.
    But we call `torch.cuda.empty_cache()` (release_cache) between dictations,
    which returns those blocks to the driver — so the next replay reads freed
    memory and raises `CUDA error: an illegal memory access`, which poisons the
    process's CUDA context and makes *every* later decode fail until restart.
    Symptom: the first dictation works, the second (and all after) crash.

    Disabling the graph decoder falls back to the eager label loop. For the short
    segments this server decodes the speed difference is negligible, and it removes
    the crash entirely (verified on RTX 5090 / sm_120). No-op for CTC models, which
    have no `greedy.use_cuda_graph_decoder`.
    """
    try:
        from omegaconf import open_dict

        dcfg = getattr(model, "cfg", None)
        dcfg = getattr(dcfg, "decoding", None)
        if dcfg is None or "greedy" not in dcfg:
            return
        with open_dict(dcfg):
            dcfg.greedy.use_cuda_graph_decoder = False
        model.change_decoding_strategy(dcfg)
        print("[asr] cuda-graph decoder disabled (avoids replay-after-empty_cache crash)", flush=True)
    except Exception as e:  # never let a decoding-config quirk block startup
        print(f"[asr] warn: could not disable cuda-graph decoder: {e}", flush=True)


def _quiet_nemo_transcribe_warning():
    """Drop NeMo's per-call `_transcribe_output_processing is deprecated` notice.

    NeMo forces its logger to WARNING for the duration of every transcribe()
    call (asr .../transcription.py), so raising the level can't suppress it.
    A logging filter on the underlying logger drops just this record, at any
    level, leaving genuine warnings intact.
    """
    import logging as pylog

    class _DropDeprecation(pylog.Filter):
        def filter(self, record):
            return "_transcribe_output_processing" not in record.getMessage()

    logger = pylog.getLogger("nemo_logger")
    if not any(isinstance(f, _DropDeprecation) for f in logger.filters):
        logger.addFilter(_DropDeprecation())


def _extract_text(result) -> str:
    """NeMo returns a list of Hypothesis|str, or a (best, all) tuple thereof."""
    hyps = result[0] if isinstance(result, tuple) else result
    item = hyps[0] if isinstance(hyps, (list, tuple)) else hyps
    text = getattr(item, "text", None)
    return (text if text is not None else str(item)).strip()
