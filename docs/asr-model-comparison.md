# ASR model comparison: Parakeet TDT v3 vs Nemotron 3.5 ASR

Blurt's server runs **`parakeet-tdt-0.6b-v3`** in bf16 over VAD-segmented
audio: Silero splits speech into utterances at silences, the active segment
is re-decoded from the start every ~350 ms for live partials, and the
segment is committed with a final full decode on a pause. In June 2026
NVIDIA released
**Nemotron 3.5 ASR Streaming 0.6B**, effectively the streaming successor to
the Parakeet line. This doc compares the two and records why Blurt stays on
Parakeet for now.

*Written August 2026 — WER figures and NeMo versions are as of that date.*

## The short version

Both are 600M-parameter FastConformer models from the same NVIDIA speech
team. The trade is: Parakeet v3 is slightly more accurate on whole
utterances, while Nemotron adds true incremental streaming, twice the
language coverage, and automatic language detection. For Blurt's current
architecture, Parakeet is the better fit; Nemotron becomes compelling if
the re-decode partial loop ever becomes a bottleneck or per-locale
Portuguese support matters.

## Side by side

| | Parakeet TDT 0.6B v3 (current) | Nemotron 3.5 ASR Streaming 0.6B |
|---|---|---|
| Architecture | FastConformer-TDT, offline | FastConformer-CacheAware-RNNT, streaming |
| Languages | 25 (European) | 40 locales, incl. **pt-PT and pt-BR as separate locales**, with auto language ID |
| English WER (Open ASR leaderboard) | ~6.3% | ~6.9% (streaming) |
| Portuguese WER (FLEURS) | 4.76% | 5.48% at 1.12s chunks, ~5.8% at 320ms |
| Latency model | Partials every ~350 ms via full re-decode of the active segment; final on VAD pause | True incremental decode, configurable 80ms–1.12s chunks, sub-100ms finals possible |
| Punctuation/caps, timestamps | Yes / yes | Yes / (streaming partials instead) |
| License | CC-BY-4.0 | OpenMDW-1.1 (also permissive) |
| NeMo required | 2.4 (what we run) | NeMo 26.06 — a substantial dependency upgrade |

## What this means for Blurt

**Accuracy: Parakeet still wins for our use case.** The WER gap (6.3% vs
6.9% English, 4.76% vs 5.48% Portuguese) exists because Nemotron commits to
words as audio arrives while Parakeet sees the whole recording. Blurt's
committed text always comes from a full-segment decode (and the final
transcript from a whole-dictation re-decode on stop), so we get
offline-model accuracy today, and Nemotron would be a small accuracy
*regression* on committed text.

**Latency: Blurt already has partials — Nemotron would make them cheaper
and faster.** Today's partials come from re-decoding the entire active
segment every ~350 ms, so per-partial cost grows with utterance length and
the cadence is bounded by decode time. Nemotron's cache-aware streaming
decodes incrementally — constant work per chunk, partials as fast as every
80 ms, and memory bounded by the chunk rather than the longest utterance.
That's a real efficiency win for long dictations, but Parakeet's RTF on a
5090 (~70 ms for a 35 s clip) means the re-decode approach is nowhere near
its limits for single-user use. Nemotron's headline scaling numbers
(thousands of concurrent streams per H100) don't matter for a local
server.

**Portuguese is a genuine point in Nemotron's favor.** Parakeet v3 has one
generic "Portuguese"; Nemotron distinguishes pt-PT from pt-BR and can
auto-detect the spoken language per utterance, which would remove any need
to configure language at all. The ~0.7pp WER difference on FLEURS may be
worth it if pt-PT-specific handling matters in practice.

**Cost of switching:** NeMo 2.4 → 26.06 is a big jump for
`requirements.in` and the Docker image, and the bf16 checkpoint export flow
(`lightware-dev/parakeet-tdt-0.6b-v3-bf16`) would need redoing. There's
also a `nemo-speech.cpp` GGUF path now, which could be interesting as a
CPU/edge fallback, though the Mac client already has Apple's on-device
SpeechTranscriber for that role.

## Decision

Stay on Parakeet v3 for the current VAD-segmented design — it's more
accurate and simpler. Revisit Nemotron if either:

1. the re-decode partial loop becomes a bottleneck (much longer
   utterances, weaker GPUs, or multiple concurrent users), or
2. pt-PT locale handling / auto language switching proves valuable.

If we do switch, keep the Silero VAD anyway to gate silence, and keep the
final full-decode pass — the protocol's partial/final shape already fits a
streaming model.

## Sources

- [nemotron-3.5-asr-streaming-0.6b model card](https://huggingface.co/nvidia/nemotron-3.5-asr-streaming-0.6b)
- [parakeet-tdt-0.6b-v3 model card](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3)
- [NVIDIA blog: scaling voice agents with cache-aware streaming ASR](https://huggingface.co/blog/nvidia/nemotron-speech-asr-scaling-voice-agents)
- [MarkTechPost release coverage](https://www.marktechpost.com/2026/06/06/nvidia-releases-nemotron-3-5-asr-a-600m-parameter-cache-aware-streaming-model-transcribing-40-language-locales-in-real-time/)
