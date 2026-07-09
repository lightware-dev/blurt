"""
Parakeet ASR engine — a thin, fast wrapper around NVIDIA NeMo's
parakeet-tdt-0.6b-v3 model for in-memory streaming decode.

Design goals: minimal VRAM (bf16, ~1.5-2 GB), low WER (full-context decode of
each speech segment), and no per-call disk I/O (we feed numpy arrays straight to
the model instead of writing a temp wav every tick like a naive prototype).
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

from server.models import resolve as resolve_model

DEFAULT_MODEL = resolve_model(os.getenv("PARAKEET_MODEL"))


class ParakeetASR:
    """Loads the model once and serialises decode calls behind a lock.

    NeMo transcribe is synchronous and not thread-safe for concurrent calls on
    one model instance, so a single lock guards it. Callers should run
    `transcribe` in a worker thread (asyncio.to_thread) to keep the event loop
    free.
    """

    def __init__(self, model_name: str | None = None, fp32: bool | None = None):
        self.model_name = resolve_model(model_name) if model_name else DEFAULT_MODEL
        self.fp32 = (os.getenv("PARAKEET_FP32") == "1") if fp32 is None else fp32
        self._model = None
        self._lock = threading.Lock()
        self.dtype = None
        self._torch = None

    def load(self):
        if self._model is not None:
            return self._model
        import torch
        import nemo.collections.asr as nemo_asr

        t0 = time.time()
        print(f"[asr] loading {self.model_name} ...", flush=True)
        model = nemo_asr.models.ASRModel.from_pretrained(self.model_name)
        model.eval()
        if torch.cuda.is_available():
            model.to("cuda")
            # bf16 halves VRAM with output identical to fp32 for this model.
            if not self.fp32 and torch.cuda.is_bf16_supported():
                model.to(torch.bfloat16)
        self.dtype = next(model.parameters()).dtype
        _disable_cuda_graph_decoder(model)
        self._model = model
        self._torch = torch
        _quiet_nemo_transcribe_warning()
        print(f"[asr] ready in {time.time()-t0:.1f}s (dtype={self.dtype})", flush=True)
        return model

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
