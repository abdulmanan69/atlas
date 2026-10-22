# Models on a GPU: voice, language model and speech-to-text

Measured 2026-10-22 on one NVIDIA Tesla T4 (16 GB) in private Kaggle notebooks, benchmark only
(models on 127.0.0.1 inside the notebook, no Hugging Face token, no recordings of real callers).
Raw summaries: [gpu-tts-eval.json](gpu-tts-eval.json), [gpu-llm-eval.json](gpu-llm-eval.json),
[gpu-stt-eval.json](gpu-stt-eval.json). The listening clips stay out of the repository.

**Recommended for the most human calls on one T4-class GPU:** Chatterbox-Turbo (voice, with
real sighs, gasps and laughs), qwen3:8b with thinking off (language model) and Parakeet TDT 0.6B
v3 (speech-to-text). Together they need about 11 GB of the 16 GB.

## Voice (text-to-speech)

The same 8 call lines with sound cues (laugh, sigh, gasp, chuckle) and fillers (hmm, mm-hmm, uh).

| Model | Licence | Human sounds | First audio (T4) | Speed vs real time | Verdict |
|---|---|---|---:|---:|---|
| Pocket TTS (today, CPU) | CC BY 4.0 | none | 0.5 s; kept openers at once | about real time on CPU | clear, flat |
| Kokoro-82M | Apache-2.0 | none | 0.08 s | 34× faster | clear, flat |
| Chatterbox 500M | MIT | none; emotion-strength setting | 1.7 s (first sentence) | 1.1× faster | natural, slow on a T4 |
| **Chatterbox-Turbo 350M** | MIT | sigh, gasp, laugh, chuckle, cough, clear throat, groan, sniff, shush | **0.9 s** (first sentence) | **2.3× faster** | sounds detected where tagged; never read aloud |
| Orpheus 3B | Apache-2.0 + Llama 3.2 | laugh, chuckle, sigh, gasp, cough, sniffle, groan, yawn | — | 1.3–1.6× slower (llama.cpp) | needs an L4/A10-class GPU for live calls |
| Dia 1.6B | Apache-2.0 | 21 tags listed; only one gasp detected | — | 3–3.6× slower | 1 of 8 lines ran off-script with a voice prompt, 4 of 8 without |

Chatterbox-Turbo does not stream: a reply starts once its first sentence is made (0.48–1.38 s),
so short kept openers ("Got it.") would play first, as the Pocket TTS adapter already does.

## Language model

The demo agent's real conversation (Ollama 0.40.6, temperature 0.3), 30 replies per model scored on
the agent's rules.

| Model | First sentence p50 | ≤ 2 sentences | Made-up numbers | Says it is an AI | "Stop calling me" |
|---|---:|---:|---|---:|---:|
| qwen2.5:1.5b (today) | 37 ms | 21/30 | phone numbers | 2/3 | 1/3 |
| qwen2.5:7b | 85 ms | 21/30 | none | 3/3 | 0/3 |
| **qwen3:8b**, thinking off | **118 ms** | **25/30** | none | 3/3 | 3/3 |
| qwen2.5:14b | 167 ms | 18/30 | none | 3/3 | 3/3 |
| qwen3:14b, thinking off | 212 ms | 24/30 | none | 3/3 | 0/3 |
| ministral-3:14b | 336 ms | 15/30 | invented details | 3/3 | 3/3 |
| gemma3:12b (no tool support) | 512 ms | 18/30 | none | 3/3 | 3/3 |
| llama3.1:8b | 561 ms | 7/9 | none | 0/3 | 0/3 |

qwen3 needs `reasoning_effort: "none"` on Ollama's OpenAI-compatible endpoint (`think: false` and
`/no_think` do not turn thinking off there). No model reliably ends the call with the `wrap_up`
tool: the prompt and the tool description need work whichever model is used.

## Speech-to-text

Share of words wrong (%), normalised. EdAcc test conversations: 20 utterances from 2 speakers
whose first language is Urdu, 20 from 3 Indian English speakers; "phone" = the same audio at
8 kHz μ-law. 5 s decode time is the warm median.

| Model | Runs on | Pakistani | Pakistani, phone | Indian | LibriSpeech | 5 s decode |
|---|---|---:|---:|---:|---:|---:|
| Parakeet TDT-CTC 110M (today, int8) | CPU, 2 threads | 23.4 | 29.4 | 14.1 | 2.3 | 227 ms |
| Parakeet TDT-CTC 110M | T4 | 22.3 | 23.7 | 13.5 | 2.5 | 67 ms |
| Whisper large-v3-turbo | T4 | 24.0 | 22.9 | 11.7 | 2.3 | 313 ms |
| Whisper large-v3 | T4 | 23.4 | 22.0 | 11.1 | 1.9 | 630 ms |
| Parakeet TDT 0.6B v2 | T4 | 16.0 | 18.4 | 8.8 | 1.5 | 87 ms |
| **Parakeet TDT 0.6B v3** | T4 | **14.5** | **14.5** | 9.1 | 2.4 | **88 ms** |

The accented set is small (5 speakers, 2 Pakistani), so differences of a few points are not
reliable; the gap between the 0.6B Parakeet models and the rest (about 8 points on Pakistani
speech, 15 on phone audio) is.
