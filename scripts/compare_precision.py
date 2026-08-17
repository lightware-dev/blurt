#!/usr/bin/env python3
"""Compare Parakeet decode quality and speed across weight precisions.

Answers the question "would fp16 cost me anything relative to bf16?" with three
independent kinds of evidence, because text alone can hide a numerically sick model
(and a numerically different model can still emit identical text):

  1. WER / CER against ground truth, per corpus slice, with a paired bootstrap CI
     on the bf16-minus-fp16 difference — the difference is what matters, not either
     absolute number.
  2. Encoder-output divergence from an fp32 reference run of the same model
     (relative L2, max abs error, cosine similarity, non-finite counts). This is
     what actually distinguishes the formats: fp16 has 3 more mantissa bits, bf16
     has far more exponent range, and only measurement says which one this model's
     activations care about.
  3. Wall-clock RTF and peak VRAM per precision.

Each precision is loaded, measured and freed in turn, so peak VRAM is per-model.

Usage:
    python scripts/compare_precision.py --corpus /tmp/corpus --out results.json
    python scripts/compare_precision.py --corpus /tmp/corpus --no-reference  # skip fp32
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server.asr  # noqa: E402  (applies the NumPy 2 sctypes shim)
from server.asr import SAMPLE_RATE, ParakeetASR  # noqa: E402

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]


def _num_to_words(n: int) -> str:
    """Spell out a non-negative integer, enough for eval normalisation."""
    if n < 20:
        return _ONES[n]
    if n < 100:
        return _TENS[n // 10] + ("" if n % 10 == 0 else " " + _ONES[n % 10])
    if n < 1000:
        rest = "" if n % 100 == 0 else " " + _num_to_words(n % 100)
        return _ONES[n // 100] + " hundred" + rest
    for div, name in ((1_000_000_000, "billion"), (1_000_000, "million"), (1000, "thousand")):
        if n >= div:
            rest = "" if n % div == 0 else " " + _num_to_words(n % div)
            return _num_to_words(n // div) + " " + name + rest
    return str(n)


def normalise(text: str) -> str:
    """Lowercase, drop punctuation, spell out integers.

    LibriSpeech references are unpunctuated upper case with numbers written out,
    while the model emits punctuation, casing and digits; without this the score
    would mostly measure formatting. Both precisions get the identical treatment,
    so any residual normalisation error cancels in the comparison.
    """
    text = text.lower().replace("’", "'")
    text = re.sub(r"(\d),(\d)", r"\1\2", text)
    text = re.sub(r"\d+", lambda m: " " + _num_to_words(int(m.group())) + " ", text)
    text = re.sub(r"[^a-z' ]+", " ", text)
    return " ".join(text.split())


def _levenshtein(ref: list, hyp: list) -> int:
    """Edit distance over token lists (two-row DP; sequences here are short)."""
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return prev[-1]


def score(ref: str, hyp: str) -> dict:
    """Word and character edit counts for one utterance."""
    r, h = normalise(ref), normalise(hyp)
    rw, hw = r.split(), h.split()
    return {
        "word_errors": _levenshtein(rw, hw), "ref_words": len(rw),
        "char_errors": _levenshtein(list(r), list(h)), "ref_chars": len(r),
        "exact": r == h,
    }


def load_wav(path: str) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != SAMPLE_RATE:
        import librosa
        audio = librosa.resample(audio, orig_sr=sr, target_sr=SAMPLE_RATE)
    return np.ascontiguousarray(audio, dtype=np.float32)


def load_model(precision: str):
    """Return (model, torch) for a precision; 'fp32' pulls the upstream checkpoint."""
    import torch

    if precision != "fp32":
        asr = ParakeetASR(precision=precision)
        asr.load()
        return asr, torch

    # fp32 reference: the unquantised upstream weights, straight from the HF cache.
    import nemo.collections.asr as nemo_asr
    from server.asr import MODEL_ID, _disable_cuda_graph_decoder

    print(f"[cmp] loading fp32 reference {MODEL_ID} ...", flush=True)
    model = nemo_asr.models.ASRModel.from_pretrained(MODEL_ID, map_location="cuda")
    model.eval()
    _disable_cuda_graph_decoder(model)

    class _Fp32Wrapper:  # same surface as ParakeetASR, minus the checkpoint plumbing
        precision, dtype = "fp32", torch.float32

        def __init__(self, m):
            self._model = m

        def transcribe(self, audio):
            with torch.inference_mode():
                return server.asr._extract_text(
                    self._model.transcribe([audio], batch_size=1, verbose=False))

    return _Fp32Wrapper(model), torch


def encoder_output(model, torch, audio: np.ndarray, dtype) -> np.ndarray:
    """Run preprocessor + encoder and return the encoding as float32 on the CPU.

    The encoder output is the model's whole acoustic opinion before the decoder's
    argmax throws information away, so comparing it detects precision damage that
    greedy decoding would round off into an identical transcript.
    """
    with torch.inference_mode():
        sig = torch.from_numpy(audio).to("cuda", dtype=dtype).unsqueeze(0)
        length = torch.tensor([sig.shape[-1]], device="cuda")
        encoded, _ = model(input_signal=sig, input_signal_length=length)
    return encoded.detach().float().cpu().numpy()


def divergence(ref: np.ndarray, got: np.ndarray) -> dict:
    """How far an encoding strayed from the fp32 reference encoding.

    Encodings are [batch, features, frames]; trimming has to happen on the frame
    axis. Flattening first and truncating would silently compare frame *n* of one
    run against frame *n+1* of the other whenever the two disagree by a frame, and
    report the resulting misalignment as precision damage.
    """
    frames = min(ref.shape[-1], got.shape[-1])
    r = ref[..., :frames].astype(np.float64).ravel()
    g = got[..., :frames].astype(np.float64).ravel()
    denom = float(np.linalg.norm(r)) or 1e-12
    cos = float(r @ g / ((np.linalg.norm(r) or 1e-12) * (np.linalg.norm(g) or 1e-12)))
    return {
        "rel_l2": float(np.linalg.norm(r - g) / denom),
        "max_abs": float(np.max(np.abs(r - g))) if r.size else 0.0,
        "cosine": cos,
        "nonfinite": int(np.count_nonzero(~np.isfinite(got))),
        "max_abs_activation": float(np.max(np.abs(g))) if g.size else 0.0,
        "frames_ref": int(ref.shape[-1]),
        "frames_got": int(got.shape[-1]),
    }


def run_precision(precision: str, items: list, audio: dict, want_encodings: bool,
                  timing_ids: list, timing_repeats: int, reload_every: int = 0,
                  release_cache: bool = False) -> dict:
    """Load one precision, decode the corpus, and free it again.

    Pass order is load-bearing, for one blunt reason: NeMo's transcribe() leaves the
    model in a state where a *direct* forward call (what encoder_output does) returns
    nondeterministic garbage. On a freshly loaded model two identical forward passes
    agree exactly; after a single transcribe() they disagree by ~60% relative L2, and
    interleaving the two kinds of call also makes transcribe() itself start returning
    empty transcripts (3/208 clips when the passes are separated, 18-26/208 when
    interleaved). transcribe() on its own is unaffected — the server only ever calls
    that — but this harness uses both, so it captures every encoding first, on a
    never-transcribed model, then reloads before decoding a word.
    """
    import gc

    model_holder, torch = load_model(precision)
    inner = model_holder._model
    dtype = model_holder.dtype
    torch.cuda.synchronize()
    torch.cuda.reset_peak_memory_stats()
    weights_vram = torch.cuda.memory_allocated() / 1e9
    reloads = 0
    results, encodings = {}, {}

    # Encodings first, while the model has never transcribed (see the docstring).
    selfcheck = None
    if want_encodings:
        # Prove determinism before trusting any of it: the same clip encoded twice
        # must agree bit for bit, or every divergence number below is noise.
        probe = audio[items[0]["id"]]
        selfcheck = divergence(encoder_output(inner, torch, probe, dtype),
                               encoder_output(inner, torch, probe, dtype))["rel_l2"]
        if selfcheck > 0:
            print(f"[cmp] WARNING {precision}: encoder is nondeterministic "
                  f"(rel_l2={selfcheck:.5f}); divergence figures are not meaningful",
                  flush=True)
        for n, item in enumerate(items, 1):
            encodings[item["id"]] = encoder_output(inner, torch, audio[item["id"]], dtype)
            if n % 50 == 0:
                print(f"[cmp] {precision}: {n}/{len(items)} encoded", flush=True)
        del model_holder, inner
        gc.collect()
        torch.cuda.empty_cache()
        model_holder, _ = load_model(precision)
        inner = model_holder._model

    # Warm up: the first decode pays for lazy kernel/JIT setup and would otherwise
    # land entirely on whichever clip happens to be first.
    model_holder.transcribe(audio[items[0]["id"]])

    t_start = time.time()
    for n, item in enumerate(items, 1):
        if reload_every and n > 1 and (n - 1) % reload_every == 0:
            del model_holder, inner
            gc.collect()
            torch.cuda.empty_cache()
            model_holder, _ = load_model(precision)
            inner = model_holder._model
            reloads += 1
        wav = audio[item["id"]]
        t0 = time.time()
        text = model_holder.transcribe(wav)
        dt = time.time() - t0
        results[item["id"]] = {"text": text, "seconds": item["seconds"], "decode_s": dt}
        if release_cache:
            torch.cuda.empty_cache()
        if n % 25 == 0:
            print(f"[cmp] {precision}: {n}/{len(items)} decoded", flush=True)

    wall_s = time.time() - t_start

    # Timing next, still before the encoder capture, so the RTF median is measured
    # on a model in the same state the server's would be in.
    rtfs = {}
    for uid in timing_ids:
        wav = audio[uid]
        dur = len(wav) / SAMPLE_RATE
        samples = []
        for _ in range(timing_repeats):
            torch.cuda.synchronize()
            t0 = time.time()
            model_holder.transcribe(wav)
            torch.cuda.synchronize()
            samples.append((time.time() - t0) / dur)
        rtfs[uid] = float(np.median(samples))

    peak_vram = torch.cuda.max_memory_allocated() / 1e9
    total_audio = sum(i["seconds"] for i in items)
    empties = [i["id"] for i in items if not results[i["id"]]["text"].strip()]
    out = {
        "precision": precision,
        "dtype": str(dtype),
        "reloads": reloads,
        "empty_transcripts": empties,
        "encoder_selfcheck_rel_l2": selfcheck,
        "weights_vram_gb": weights_vram,
        "peak_vram_gb": peak_vram,
        "wall_s": wall_s,
        "audio_s": total_audio,
        "rtf_median": float(np.median(list(rtfs.values()))) if rtfs else None,
        "rtf_per_clip": rtfs,
        "results": results,
    }
    del model_holder, inner
    gc.collect()
    torch.cuda.empty_cache()
    return out, encodings


def corpus_wer(items: list, run: dict) -> dict:
    """Aggregate WER/CER over a set of items for one precision run."""
    we = rw = ce = rc = exact = 0
    for item in items:
        s = score(item["text"], run["results"][item["id"]]["text"])
        we += s["word_errors"]; rw += s["ref_words"]
        ce += s["char_errors"]; rc += s["ref_chars"]
        exact += int(s["exact"])
    return {"wer": we / max(rw, 1), "cer": ce / max(rc, 1),
            "word_errors": we, "ref_words": rw, "exact_rate": exact / max(len(items), 1),
            "n": len(items)}


def paired_bootstrap(items: list, run_a: dict, run_b: dict, iters: int = 5000,
                     seed: int = 7) -> dict:
    """95% CI on (WER_a - WER_b), resampling utterances in pairs.

    Paired because both precisions decode the identical clips: the utterance-level
    noise is shared and cancels, which a two-sample test would leave in.
    """
    rng = np.random.default_rng(seed)
    per = []
    for item in items:
        sa = score(item["text"], run_a["results"][item["id"]]["text"])
        sb = score(item["text"], run_b["results"][item["id"]]["text"])
        per.append((sa["word_errors"], sb["word_errors"], sa["ref_words"]))
    arr = np.array(per, dtype=np.float64)
    idx = rng.integers(0, len(arr), size=(iters, len(arr)))
    ea, eb, words = arr[:, 0][idx].sum(1), arr[:, 1][idx].sum(1), arr[:, 2][idx].sum(1)
    diffs = ea / np.maximum(words, 1) - eb / np.maximum(words, 1)
    observed = arr[:, 0].sum() / max(arr[:, 2].sum(), 1) - arr[:, 1].sum() / max(arr[:, 2].sum(), 1)
    return {
        "observed_diff": float(observed),
        "ci95": [float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))],
        # Share of resamples that flip the sign — a CI straddling zero means the
        # corpus cannot tell the two precisions apart.
        "p_two_sided": float(2 * min((diffs <= 0).mean(), (diffs >= 0).mean())),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True, help="dir holding manifest.json")
    ap.add_argument("--out", default="precision-results.json")
    ap.add_argument("--dtypes", nargs="+", default=["bf16", "fp16"])
    ap.add_argument("--no-reference", action="store_true",
                    help="skip the fp32 reference run (halves VRAM and time)")
    ap.add_argument("--reload-every", type=int, default=50,
                    help="reload the model every N clips (0 = never); see run_precision")
    ap.add_argument("--release-cache", action="store_true",
                    help="empty the CUDA cache between clips, as the server does")
    ap.add_argument("--timing-clips", type=int, default=12)
    ap.add_argument("--timing-repeats", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0, help="cap clips (debugging)")
    args = ap.parse_args()

    items = json.loads((Path(args.corpus) / "manifest.json").read_text())
    if args.limit:
        items = items[:args.limit]
    audio = {i["id"]: load_wav(i["path"]) for i in items}
    print(f"[cmp] corpus: {len(items)} clips, "
          f"{sum(i['seconds'] for i in items)/60:.1f} min", flush=True)

    # Time a spread of durations, not whichever clips happen to come first.
    by_dur = sorted(items, key=lambda i: i["seconds"])
    timing_ids = [by_dur[int(x)]["id"] for x in
                  np.linspace(0, len(by_dur) - 1, min(args.timing_clips, len(by_dur)))]

    order = (["fp32"] if not args.no_reference else []) + list(args.dtypes)
    runs, ref_enc = {}, None
    for precision in order:
        print(f"\n[cmp] ===== {precision} =====", flush=True)
        run, enc = run_precision(precision, items, audio,
                                 want_encodings=not args.no_reference,
                                 timing_ids=timing_ids,
                                 timing_repeats=args.timing_repeats,
                                 reload_every=args.reload_every,
                                 release_cache=args.release_cache)
        if precision == "fp32":
            ref_enc = enc
        elif ref_enc:
            div = [divergence(ref_enc[i["id"]], enc[i["id"]]) for i in items]
            run["divergence"] = {
                "rel_l2_mean": float(np.mean([d["rel_l2"] for d in div])),
                "rel_l2_max": float(np.max([d["rel_l2"] for d in div])),
                "cosine_min": float(np.min([d["cosine"] for d in div])),
                "max_abs_max": float(np.max([d["max_abs"] for d in div])),
                "nonfinite_total": int(sum(d["nonfinite"] for d in div)),
                "max_abs_activation": float(np.max([d["max_abs_activation"] for d in div])),
                "frame_count_mismatches": int(sum(d["frames_ref"] != d["frames_got"]
                                                  for d in div)),
                "per_clip": {i["id"]: d for i, d in zip(items, div)},
            }
        runs[precision] = run
        del enc

    report = {"corpus": args.corpus, "n_clips": len(items), "runs": runs, "slices": {}}
    kinds = sorted({i["kind"] for i in items})
    for label, subset in [("all", items)] + [(k, [i for i in items if i["kind"] == k])
                                             for k in kinds] + \
                         [("real+degraded", [i for i in items if i["kind"] != "synthetic"])]:
        if not subset:
            continue
        report["slices"][label] = {p: corpus_wer(subset, runs[p]) for p in runs}

    if len(args.dtypes) == 2:
        a, b = args.dtypes
        report["bootstrap"] = {
            f"{a}_minus_{b}": paired_bootstrap(items, runs[a], runs[b]),
            "real_only": paired_bootstrap([i for i in items if i["kind"] != "synthetic"],
                                          runs[a], runs[b]),
        }
        agree = sum(normalise(runs[a]["results"][i["id"]]["text"])
                    == normalise(runs[b]["results"][i["id"]]["text"]) for i in items)
        report["agreement"] = {"identical_after_normalisation": agree,
                               "of": len(items), "rate": agree / max(len(items), 1)}
        report["disagreements"] = [
            {"id": i["id"], "kind": i["kind"], "ref": i["text"],
             a: runs[a]["results"][i["id"]]["text"], b: runs[b]["results"][i["id"]]["text"]}
            for i in items
            if normalise(runs[a]["results"][i["id"]]["text"])
            != normalise(runs[b]["results"][i["id"]]["text"])]

    Path(args.out).write_text(json.dumps(report, indent=2))
    print_report(report, args.dtypes)
    print(f"\n[cmp] full results -> {args.out}")


def print_report(report: dict, dtypes: list):
    runs = report["runs"]
    order = list(runs)
    print("\n=== WER / CER by slice ===")
    head = f"{'slice':22s} {'n':>4s}  " + "  ".join(f"{p:>18s}" for p in order)
    print(head)
    for label, per in report["slices"].items():
        n = per[order[0]]["n"]
        cells = "  ".join(
            f"{per[p]['wer']*100:8.2f}% /{per[p]['cer']*100:6.2f}%" for p in order)
        print(f"{label:22s} {n:4d}  {cells}")

    print("\n=== speed / memory ===")
    for p in order:
        r = runs[p]
        print(f"{p:6s} rtf_median={r['rtf_median']:.5f}  "
              f"weights={r['weights_vram_gb']:.2f} GB  peak={r['peak_vram_gb']:.2f} GB  "
              f"wall={r['wall_s']:.0f}s for {r['audio_s']/60:.1f} min audio")

    print("\n=== empty transcripts (precision-independent decoder artefact) ===")
    for p in order:
        r = runs[p]
        print(f"{p:6s} {len(r['empty_transcripts']):3d}/{report['n_clips']} empty "
              f"(model reloads: {r['reloads']})")

    if any("divergence" in runs[p] for p in order):
        print("\n=== encoder divergence from fp32 ===")
        for p in order:
            d = runs[p].get("divergence")
            if d:
                print(f"{p:6s} rel_l2 mean={d['rel_l2_mean']:.5f} max={d['rel_l2_max']:.5f}  "
                      f"cos_min={d['cosine_min']:.6f}  max_abs={d['max_abs_max']:.4f}  "
                      f"nonfinite={d['nonfinite_total']}  "
                      f"frame_mismatch={d['frame_count_mismatches']}")
        for p in order:
            sc = runs[p].get("encoder_selfcheck_rel_l2")
            if sc is not None:
                print(f"{p:6s} self-check (same clip twice): rel_l2={sc:.6f}"
                      + ("" if sc == 0 else "  <-- NONDETERMINISTIC, figures above are noise"))

    if "agreement" in report:
        a = report["agreement"]
        print(f"\n=== {dtypes[0]} vs {dtypes[1]} ===")
        print(f"identical transcripts: {a['identical_after_normalisation']}/{a['of']} "
              f"({a['rate']*100:.1f}%)")
        for key, bs in report["bootstrap"].items():
            print(f"WER diff [{key}] {bs['observed_diff']*100:+.3f} pp  "
                  f"95% CI [{bs['ci95'][0]*100:+.3f}, {bs['ci95'][1]*100:+.3f}] pp  "
                  f"p={bs['p_two_sided']:.3f}")


if __name__ == "__main__":
    main()
