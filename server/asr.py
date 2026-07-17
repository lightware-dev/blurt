"""
Parakeet ASR engine — a thin, fast wrapper around NVIDIA NeMo's
parakeet-tdt-0.6b-v3 model for in-memory streaming decode.

Design goals: minimal VRAM (bf16, ~1.3 GB), low WER (full-context decode of
each speech segment), and no per-call disk I/O (we feed numpy arrays straight to
the model instead of writing a temp wav every tick like a naive prototype).

We load a bf16 .nemo (see bf16_ckpt_path) directly onto the GPU in ~13 s, never
materialising an fp32 copy. The checkpoint comes from, in order:
  1. a local cache file (from a prior run or scripts/build_bf16_ckpt.py),
  2. else a direct download of the pre-built bf16 .nemo we publish on HF (BF16_REPO).
If neither is available, load() raises — the server does not fall back to fetching
and converting the upstream fp32 checkpoint. Pre-build a local one offline with
scripts/build_bf16_ckpt.py.
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

# The one model we support: multilingual 0.6B TDT, run in bf16 on the GPU.
MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"

# Pre-built bf16 .nemo we publish so first run downloads it directly instead of
# pulling the ~2.4 GB fp32 checkpoint and casting it (saves the one-off convert and
# the fp32 RAM transient). Override with PARAKEET_BF16_REPO to host your own.
BF16_REPO = os.getenv("PARAKEET_BF16_REPO") or "lightware-dev/parakeet-tdt-0.6b-v3-bf16"
BF16_FILE = "parakeet-tdt-0.6b-v3-bf16.nemo"


class ParakeetASR:
    """Loads the model once and serialises decode calls behind a lock.

    NeMo transcribe is synchronous and not thread-safe for concurrent calls on
    one model instance, so a single lock guards it. Callers should run
    `transcribe` in a worker thread (asyncio.to_thread) to keep the event loop
    free.
    """

    def __init__(self):
        self.model_name = MODEL_ID
        self._model = None
        self._lock = threading.Lock()
        self.dtype = None
        self._torch = None

    def bf16_ckpt_path(self) -> str:
        """Where the pre-converted bf16 .nemo lives (override with PARAKEET_BF16_CKPT)."""
        return os.getenv("PARAKEET_BF16_CKPT") or os.path.expanduser(
            "~/.cache/blurt/parakeet-tdt-0.6b-v3-bf16.nemo")

    def load(self):
        if self._model is not None:
            return self._model
        import torch
        import nemo.collections.asr as nemo_asr

        t0 = time.time()
        if not (torch.cuda.is_available() and torch.cuda.is_bf16_supported()):
            raise RuntimeError(
                "Blurt only supports parakeet-tdt-0.6b-v3 in bf16 on a CUDA GPU; "
                "no bf16-capable CUDA device was found.")
        ckpt = self.bf16_ckpt_path()
        if not os.path.exists(ckpt):
            ckpt = self._download_bf16_ckpt() or ""
        if not ckpt or not os.path.exists(ckpt):
            raise RuntimeError(
                f"No bf16 checkpoint available: neither a local cache "
                f"({self.bf16_ckpt_path()}) nor a download from {BF16_REPO} succeeded. "
                "Check the network / HF_TOKEN, or pre-build one with "
                "scripts/build_bf16_ckpt.py.")

        # The bf16 checkpoint loads straight onto the GPU with no fp32 ever
        # materialising. set_default_dtype makes NeMo build the params bf16 directly
        # (peak ~1.3 GB, vs a ~2.5 GB fp32 transient if restored as fp32 then cast);
        # the following to(bfloat16) also converts preprocessor buffers so the
        # featurizer's output dtype matches the bf16 convs (else the mel features come
        # out fp32 and the first conv raises a dtype mismatch).
        print(f"[asr] loading bf16 checkpoint {ckpt} ...", flush=True)
        torch.set_default_dtype(torch.bfloat16)
        try:
            model = nemo_asr.models.ASRModel.restore_from(ckpt, map_location="cuda")
        finally:
            torch.set_default_dtype(torch.float32)
        model.eval()
        model.to(torch.bfloat16)

        self.dtype = next(model.parameters()).dtype
        _disable_cuda_graph_decoder(model)
        self._model = model
        self._torch = torch
        _quiet_nemo_transcribe_warning()
        print(f"[asr] ready in {time.time()-t0:.1f}s (dtype={self.dtype})", flush=True)
        return model

    @staticmethod
    def _download_bf16_ckpt():
        """Best-effort fetch of the pre-built bf16 .nemo from HF (BF16_REPO).

        Returns a local path to the checkpoint, or None on any failure (missing repo,
        no network, private repo without auth); load() raises if it gets None and has no
        local cache. The file is served from huggingface_hub's own cache, so later starts
        return it without re-downloading; set HF_TOKEN to access a private mirror.
        """
        try:
            from huggingface_hub import hf_hub_download
        except Exception:
            return None
        try:
            print(f"[asr] fetching pre-built bf16 checkpoint {BF16_REPO}/{BF16_FILE} ...", flush=True)
            path = hf_hub_download(BF16_REPO, BF16_FILE)
            print(f"[asr] downloaded bf16 checkpoint -> {path}", flush=True)
            return path
        except Exception as e:  # load() turns a None into a clear no-checkpoint error
            print(f"[asr] warn: could not fetch bf16 checkpoint ({e})", flush=True)
            return None

    @staticmethod
    def _save_bf16_ckpt(model, ckpt: str):
        """Best-effort save of the bf16 model so subsequent starts load it directly."""
        try:
            os.makedirs(os.path.dirname(ckpt), exist_ok=True)
            model.save_to(ckpt)
            print(f"[asr] cached bf16 checkpoint -> {ckpt}", flush=True)
        except Exception as e:  # a cache miss is not worth failing startup over
            print(f"[asr] warn: could not cache bf16 checkpoint: {e}", flush=True)

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
