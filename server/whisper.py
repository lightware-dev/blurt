"""
Whisper ASR engine — the alternative to the default Parakeet engine, selected
with BLURT_ASR_ENGINE=whisper. One engine runs per process (see server/engine.py);
they are never loaded side by side, so VRAM stays that of a single model.

It runs OpenAI's Whisper through 🤗 transformers, which is already in the
dependency tree (NeMo pulls it in) and reuses the torch build the container
ships. The alternative runtimes (faster-whisper/CTranslate2, whisper.cpp) are
quicker, but each brings its own CUDA runtime beside torch's — a second copy of
cuBLAS/cuDNN in the image, built for a different CUDA major than the cu130
wheels here. Not worth it for a model that already decodes a dictation segment
in well under its duration.

Model: `openai/whisper-large-v3-turbo` by default — 809 M params, ~1.6 GB in
half precision, so roughly the footprint of the Parakeet default, with a
four-layer decoder that keeps it fast. WHISPER_MODEL takes any Whisper
checkpoint on the Hub (`openai/whisper-large-v3`, `openai/whisper-small`, a
fine-tune of your own). Weights come from the HuggingFace cache, so they land in
the same ~/.cache the container already persists.

Precision mirrors the Parakeet engine: bf16 by default, `WHISPER_DTYPE=fp16` for
pre-Ampere cards (sm_75) that have no bf16 at all, and `WHISPER_DTYPE=nf4` for
4-bit weights when VRAM is what you are short of.

The 4-bit format is **NF4** through bitsandbytes, not the NVFP4 snapshot the
Parakeet engine ships, and the reason is measurement rather than taste. Over the
repo's 208-clip corpus both hold WER within noise and both cut the weights to
about a third, but NVFP4 costs 4.1x the decode latency here against NF4's 1.7x:
the modelopt path unpacks each weight back to bf16 and calls an ordinary GEMM,
while bitsandbytes fuses the dequantization into its matmul. Parakeet does not
pay that because its encoder is bound by kernel launches rather than arithmetic;
Whisper's decoder, generating a token at a time, pays in full.

NF4 also needs no snapshot. modelopt quantizes from the loaded bf16 weights, so
building it peaks *above* the model it replaces (1.8 GB against 1.6 GB) — useless
on a card that could not hold bf16 in the first place, which is why
server/nvfp4.py exists. bitsandbytes quantizes layer by layer as the checkpoint
streams in and peaks at 0.81 GB, so there is nothing to pre-build, calibrate or
host: WHISPER_DTYPE=nf4 and the next start is 4-bit.

Language: Whisper auto-detects by default, exactly like Parakeet. Pin it with
WHISPER_LANGUAGE=en when you always dictate in one language — detection runs off
the first window, and on a short, noisy first segment it is the one thing that
reliably goes wrong. WHISPER_TASK=translate emits English for any input.

Long audio: Whisper's encoder sees a fixed 30 s window, and blurtd hands us
whole dictations (FINAL_MAX_S, 120 s by default) in one call. Anything past 30 s
therefore goes through transformers' sequential long-form decode, which slides
that window and stitches the pieces — see transcribe().

Short audio: Whisper pads everything out to that same 30 s window, and on a
fraction of a second of near-silence it has a documented habit of filling the
rest with something plausible — a stock phrase from its training data. The VAD
gate in front of it (VAD_THRESHOLD) and MIN_SEGMENT_S are what keep that audio
from reaching the model at all; raise them if filler text shows up in a quiet
room. Parakeet is far less prone to this, which is why the defaults were tuned
without it in mind.
"""

from __future__ import annotations

import os
import time
import threading

import numpy as np

# The rate the whole server speaks in, defined next to the default engine. Both
# engines want 16 kHz mono, so it stays one constant rather than two that could
# drift; importing it costs nothing (server/asr.py loads no model at import).
from server.asr import SAMPLE_RATE

# The default checkpoint: distilled 4-layer decoder, near large-v3 accuracy at a
# fraction of the decode cost. Override with WHISPER_MODEL.
DEFAULT_MODEL = "openai/whisper-large-v3-turbo"

# Whisper's encoder window. Audio longer than this cannot be decoded in one pass
# and takes the long-form path in transcribe().
CHUNK_S = 30

# No fp32: it doubles VRAM for no accuracy that survives a dictation. nf4 is
# weight-only — activations still run in bf16, which is why it needs the same
# Ampere-or-newer card the default does.
PRECISIONS = ("bf16", "fp16", "nf4")
DEFAULT_PRECISION = "bf16"

_PRECISION_ALIASES = {
    "bf16": "bf16", "bfloat16": "bf16",
    "fp16": "fp16", "float16": "fp16", "half": "fp16",
    # "4bit" is what the transformers flag is called, so people reach for it.
    "nf4": "nf4", "4bit": "nf4", "int4": "nf4",
}

# The language codes Whisper's multilingual checkpoints are trained on, for the
# Wyoming `info` message (Home Assistant lists them in its STT picker). Whisper
# covers roughly four times what parakeet-tdt-0.6b-v3 does; `yue` is large-v3
# and newer only, which includes the default turbo checkpoint.
LANGUAGES = [
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs",
    "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
    "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy",
    "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
    "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru",
    "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
    "yi", "yo", "yue", "zh",
]

TASKS = ("transcribe", "translate")


def resolve_precision(name: str | None) -> str:
    """Normalise a precision name ('float16' -> 'fp16'); None/'' means the default."""
    if not name:
        return DEFAULT_PRECISION
    key = _PRECISION_ALIASES.get(str(name).strip().lower())
    if key is None:
        raise ValueError(
            f"Unsupported WHISPER_DTYPE {name!r}; expected one of {sorted(PRECISIONS)}. "
            "(The 4-bit format here is nf4, via bitsandbytes; nvfp4 is Parakeet-only.)")
    return key


def resolve_language(name: str | None) -> str | None:
    """Normalise WHISPER_LANGUAGE; None (auto-detect) unless a language is pinned.

    'auto' and '' both mean detect, so a config that spells the default out
    behaves like leaving it unset. Anything else is passed to transformers as-is —
    it accepts both codes ('en') and names ('english'), and validates against the
    checkpoint's own list, which is a better error than one guessed here.
    """
    lang = (name or "").strip().lower()
    return None if lang in ("", "auto", "detect") else lang


def resolve_task(name: str | None) -> str:
    """Normalise WHISPER_TASK; 'transcribe' (keep the spoken language) by default."""
    task = (name or "").strip().lower() or "transcribe"
    if task not in TASKS:
        raise ValueError(f"Unsupported WHISPER_TASK {name!r}; expected one of {list(TASKS)}.")
    return task


class WhisperASR:
    """Loads Whisper once and serialises decode calls behind a lock.

    Same shape as ParakeetASR — load / transcribe / release_cache — because the
    server holds one of these behind a single `asr` global and never learns
    which. generate() is not safe to run concurrently on one model instance, so
    a lock guards it; callers run `transcribe` in a worker thread
    (asyncio.to_thread) to keep the event loop free.
    """

    engine = "whisper"

    def __init__(self, model: str | None = None, precision: str | None = None,
                 language: str | None = None, task: str | None = None):
        self.model_name = model or os.getenv("WHISPER_MODEL") or DEFAULT_MODEL
        # Fixed for the life of the instance, like the Parakeet engine's: the
        # weights are materialised in this dtype, so a switch means a reload.
        self.precision = resolve_precision(precision or os.getenv("WHISPER_DTYPE"))
        self.language = resolve_language(language or os.getenv("WHISPER_LANGUAGE"))
        self.task = resolve_task(task or os.getenv("WHISPER_TASK"))
        # The `.en` checkpoints (whisper-small.en and friends) are English-only and
        # *reject* the language/task arguments the multilingual ones need — passing
        # them raises rather than being ignored. The name is the only signal we have
        # before the weights are here; load() confirms it against the checkpoint.
        self.multilingual = not self.model_name.endswith(".en")
        self._model = None
        self._processor = None
        self._lock = threading.Lock()
        self.dtype = None
        self._torch = None

    @property
    def is_loaded(self) -> bool:
        """True once the model is resident and decodes will not block on a load."""
        return self._model is not None

    @property
    def description(self) -> str:
        """Human-readable model line, for the Wyoming `info` message."""
        detail = self.precision
        if lang := self._effective_language():
            detail += f", {lang}"
        if self.multilingual and self.task == "translate":
            detail += ", translate"
        return f"OpenAI Whisper — {self.model_name} ({detail})"

    @property
    def attribution(self) -> dict:
        """Who published the weights. Hub ids are org/name, so the org is the owner.

        WHISPER_MODEL also takes a local directory (an air-gapped box, a fine-tune
        you never pushed), which has no Hub page to point at — linking one anyway
        would send a Home Assistant user to a 404.
        """
        if os.path.isdir(self.model_name):
            return {"name": "local", "url": ""}
        org = self.model_name.split("/")[0] if "/" in self.model_name else "OpenAI"
        return {"name": org, "url": f"https://huggingface.co/{self.model_name}"}

    @property
    def languages(self) -> list[str]:
        """What the checkpoint can decode — one language if pinned, else all of them.

        A pinned WHISPER_LANGUAGE is a hard setting, not a hint: audio in any
        other language comes back translated or garbled. Advertising the full
        list then would invite Home Assistant to route Spanish at a server that
        only handles English.
        """
        if not self.multilingual:
            return ["en"]
        return [self.language] if self.language else list(LANGUAGES)

    def _effective_language(self) -> str | None:
        """The language actually used: an `.en` checkpoint is English whatever is set."""
        return "en" if not self.multilingual else self.language

    @property
    def model_version(self) -> str:
        """Whisper generation, from the checkpoint name ('large-v3-turbo' -> '3')."""
        name = self.model_name.rsplit("/", 1)[-1]
        for gen in ("3", "2"):
            if f"-v{gen}" in name:
                return gen
        return "1"

    def torch_dtype(self):
        """The torch dtype activations run in.

        For nf4 this is the *compute* dtype, not the weights': the packed 4-bit
        weights dequantize into it on the way into each matmul. transcribe() casts
        the mel features to this, so it has to be the activation dtype either way.
        """
        import torch

        return torch.float16 if self.precision == "fp16" else torch.bfloat16

    def _require_device(self, torch):
        """Raise with an actionable message unless this precision can run here."""
        if self.precision == "fp16":
            if not torch.cuda.is_available():
                raise RuntimeError(
                    f"Blurt only supports {self.model_name} in fp16 on a CUDA GPU; "
                    "no CUDA device was found.")
            return
        if torch.cuda.is_available() and torch.cuda.is_bf16_supported():
            return
        if self.precision == "nf4":
            raise RuntimeError(
                f"Blurt only supports {self.model_name} in nf4 on a bf16-capable CUDA "
                "GPU: the weights are 4-bit but they dequantize into bf16 to compute, "
                "so this needs Ampere (sm_80) or newer like the default does. "
                "Pre-Ampere cards (sm_75 and older) can run WHISPER_DTYPE=fp16, at "
                "full weight size.")
        raise RuntimeError(
            f"Blurt only supports {self.model_name} in bf16 on a CUDA GPU; no "
            "bf16-capable CUDA device was found. Pre-Ampere cards (sm_75 and "
            "older) can run WHISPER_DTYPE=fp16 instead.")

    def _nf4_config(self, torch):
        """The bitsandbytes recipe for WHISPER_DTYPE=nf4."""
        try:
            import bitsandbytes  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "WHISPER_DTYPE=nf4 needs bitsandbytes, which is not installed. It "
                "ships in the Docker image; from a source checkout install it with "
                "`pip install -r requirements.txt`.") from exc
        from transformers import BitsAndBytesConfig

        return BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            # Quantizes the block scales too, another ~0.4 bits per weight. Free
            # accuracy-wise at this size and it is what the measurement used.
            bnb_4bit_use_double_quant=True,
            bnb_4bit_compute_dtype=self.torch_dtype(),
            # proj_out shares its storage with the token embedding, so packing it
            # to 4 bits would take the embedding with it. It is 66 M params that
            # stay bf16 — most of why 4-bit weights land at 0.53 GB, not 0.40.
            llm_int8_skip_modules=["proj_out"],
        )

    def load(self):
        if self._model is not None:
            return self._model
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        # Inference is on the GPU, so ATen's CPU pool sits idle; leaving it at one
        # thread per core just burns thread-stack memory. Mirrors server/asr.py.
        torch.set_num_threads(int(os.getenv("OMP_NUM_THREADS", "2")))

        t0 = time.time()
        self._require_device(torch)

        dtype = self.torch_dtype()
        quantized = self.precision == "nf4"
        # Built before the line below is printed, because for nf4 this is where a
        # missing bitsandbytes is caught — and "loading ..." followed by an import
        # error reads as a download failure rather than a missing package.
        #
        # low_cpu_mem_usage streams the weights straight into half-precision params
        # instead of building an fp32 copy on the host first — what set_default_dtype
        # buys the Parakeet loader. The 4-bit path takes device_map instead, which
        # implies the same streaming and additionally quantizes each layer as it
        # lands, so the full bf16 model never exists anywhere.
        #
        # The dtype argument was renamed (torch_dtype -> dtype) around transformers
        # 4.56. Old versions reject the new name, some swallow it as an unknown
        # config kwarg and hand back fp32 — twice the VRAM, silently — so the result
        # is checked rather than trusted. We pin 4.57, where `dtype` is the name.
        kwargs = ({"quantization_config": self._nf4_config(torch), "device_map": {"": 0}}
                  if quantized else {"low_cpu_mem_usage": True})
        print(f"[asr] loading whisper {self.model_name} ({self.precision}) ...", flush=True)
        try:
            model = WhisperForConditionalGeneration.from_pretrained(
                self.model_name, dtype=dtype, **kwargs)
        except TypeError:
            model = WhisperForConditionalGeneration.from_pretrained(
                self.model_name, torch_dtype=dtype, **kwargs)
        if quantized:
            # Params are uint8 blocks now, so the dtype check below cannot apply,
            # and .to() on a 4-bit model raises — device_map already placed it.
            if not any(getattr(p, "quant_state", None) is not None
                       for p in model.parameters()):
                raise RuntimeError(
                    "bitsandbytes returned an unquantized model for WHISPER_DTYPE=nf4; "
                    "it would use full bf16 VRAM while claiming 4-bit.")
        else:
            if next(model.parameters()).dtype != dtype:
                print(f"[asr] warn: transformers ignored dtype={dtype}; casting", flush=True)
                model = model.to(dtype)
            model.to("cuda")
        model.eval()
        # Older checkpoints ship a generation config that hard-codes the decoder
        # prompt (language + task) in forced_decoder_ids. That collides with the
        # language=/task= arguments transcribe() passes, and transformers raises
        # rather than pick one. Clearing it leaves our arguments as the only
        # source, which is also what auto-detection needs.
        if getattr(model.generation_config, "forced_decoder_ids", None) is not None:
            model.generation_config.forced_decoder_ids = None

        # The checkpoint's own answer beats the name-based guess in __init__.
        self.multilingual = bool(
            getattr(model.generation_config, "is_multilingual", self.multilingual))
        if not self.multilingual and (self.language or self.task == "translate"):
            print(f"[asr] note: {self.model_name} is English-only; ignoring "
                  "WHISPER_LANGUAGE/WHISPER_TASK", flush=True)

        self._processor = WhisperProcessor.from_pretrained(self.model_name)
        self.dtype = dtype
        self._model = model
        self._torch = torch
        # For nf4 the weights are not in `dtype` — it is what they compute in — so
        # say both rather than let the line imply a bf16 model.
        detail = (f"nf4 weights, {self.dtype} compute" if quantized else f"dtype={self.dtype}")
        print(f"[asr] ready in {time.time()-t0:.1f}s ({detail})", flush=True)
        return model

    def transcribe(self, audio_f32: np.ndarray) -> str:
        """Decode a mono float32 16 kHz array to text. Returns '' for empty/silent input."""
        if audio_f32 is None or len(audio_f32) == 0:
            return ""
        model = self.load()
        torch = self._torch
        audio = np.ascontiguousarray(audio_f32, dtype=np.float32)
        # Past the encoder's 30 s window, transformers decodes sequentially:
        # window, stitch, repeat, using each window's timestamps to find the next
        # start. That path needs the unpadded, untruncated features, an attention
        # mask to find the real end, and timestamps to stitch on — hence the two
        # shapes of processor call. Short audio keeps the plain one, which pads to
        # exactly the 3000 mel frames the single-pass encoder expects.
        long_form = len(audio) > CHUNK_S * SAMPLE_RATE
        kwargs = {}
        if self.multilingual:
            # An English-only checkpoint has no language or task tokens at all and
            # raises if handed either; the multilingual ones need the task token,
            # and take the language only when it is pinned (else they detect it).
            kwargs["task"] = self.task
            if self.language:
                kwargs["language"] = self.language
        if long_form:
            kwargs["return_timestamps"] = True
            # Each window is otherwise primed with the previous window's text,
            # which turns one bad transcription into a repeating loop that eats
            # the rest of the dictation.
            kwargs["condition_on_prev_tokens"] = False

        with self._lock:
            if long_form:
                inputs = self._processor(
                    audio, sampling_rate=SAMPLE_RATE, return_tensors="pt",
                    truncation=False, padding="longest", return_attention_mask=True)
            else:
                inputs = self._processor(audio, sampling_rate=SAMPLE_RATE,
                                         return_tensors="pt")
            # Casts the float features to the model's dtype and leaves the integer
            # attention mask alone.
            inputs = inputs.to("cuda", self.dtype)
            with torch.inference_mode():
                ids = model.generate(**inputs, **kwargs)
            text = self._processor.batch_decode(ids, skip_special_tokens=True)
        return (text[0] if text else "").strip()

    def release_cache(self):
        """Return cached CUDA blocks to the driver so peak VRAM doesn't stick.

        Same reasoning as the Parakeet engine: a long segment spikes activation
        memory, and freeing it keeps steady-state VRAM near the weights.
        """
        if self._torch is not None and self._torch.cuda.is_available():
            self._torch.cuda.empty_cache()
