# Running AI models on your own hardware
To set up a complete offline demo on another PC in one command, see [Demo on another machine, fully offline](university-demo.md).

ATLAS agents need three models on every call: **speech-to-text** (hear the caller), a
**language model** (decide what to say), and **text-to-speech** (speak). Each can be a
cloud provider with your own API key, or a model running on your own servers. Self-hosted
models keep call audio and transcripts inside your network.

## What ships with ATLAS

The `local-ai` Docker profile runs OpenAI-compatible servers on CPU:

| Service | Image | Serves | Default model |
|---|---|---|---|
| `local-llm` | `ollama/ollama:0.40.6` | Language model | `qwen2.5:1.5b` (≈1 GB, Apache 2.0) |
| `local-speech` | `ghcr.io/speaches-ai/speaches:0.8.3-cpu` | Speech-to-text and Kokoro voice | Whisper base (multilingual, MIT) + Kokoro 82M (Apache 2.0) |
| `local-tts` (optional) | built from `infra/docker/pocket-tts` | Pocket TTS voice | Kyutai Pocket TTS 100M (CC BY 4.0) |
| `local-stt` (optional) | built from `infra/docker/streaming-stt` | Live English speech-to-text (Deepgram protocol) | NVIDIA Parakeet TDT-CTC 110M via sherpa-onnx (CC BY 4.0) |

Model files are stored in Docker volumes (`local_llm_models`, `local_speech_models`,
`local_tts_models`, `local_stt_models`), which live on the Docker data disk, so nothing is
written to the system drive.

## Set up (one time)

```bash
make local-ai-up        # Windows: .\make.ps1 local-ai-up
make local-ai-models    # downloads about 1.5 GB of models
make local-tts-up       # optional: builds and starts Pocket TTS (weights ≈0.3 GB on first start)
make local-stt-up       # optional: live English transcription (model ≈140 MB on first start)
make local-ai-warm      # loads every model into memory; run before a test call
```

`make local-ai-down` stops the model containers and gives their memory back.

Then in **Voice AI → AI providers**:

1. **Add connection → Self-hosted models**, name it "Local LLM", base URL
   `http://local-llm:11434/v1`.
2. **Add connection → Self-hosted models**, name it "Local speech", base URL
   `http://local-speech:8000/v1`.
3. Optional: **Add connection → Self-hosted models**, name it "Pocket TTS", base URL
   `http://local-tts:8000/v1`.
4. Under **Models for calls** choose:
   - Speech-to-text: *Local speech*, model `Systran/faster-whisper-base`
   - Language model: *Local LLM*, model `qwen2.5:1.5b`
   - Voice: *Local speech*, model `speaches-ai/Kokoro-82M-v1.0-ONNX`, voice `af_heart`,
     **or** *Pocket TTS*, model `pocket-tts`, voice `alba`
5. **Save models**, then **Test providers**.

The speech server needs the full Kokoro model id. The short name `kokoro` works with some
other Kokoro servers, but this one answers 404 for it.

For live transcription instead of Whisper, see
[Live transcription (streaming)](#live-transcription-streaming) below.

The hostnames `local-llm`, `local-speech`, `local-tts` and `local-stt` resolve inside the
Docker network that the voice service runs on, so the model servers never need to be
exposed publicly. On the host they listen only on `127.0.0.1` (ports 11434, 8001, 8002 and
8003).

## How fast is it

Measured on the development machine on 2026-10-19: Intel i5-8500T (6 cores, 2018), 12 GB
RAM, no GPU, one model container running at a time, median of 3 runs after a warm-up. Raw
results are in `docs/perf/local-*.json`.

| Stage | Model | Measured | Memory used |
|---|---|---|---|
| Speech-to-text | `Systran/faster-whisper-base` (int8) | **2.0 s** to transcribe 5 s of speech (0.40× real time) | shared with Kokoro, under 1.75 GB |
| Voice | Kokoro `speaches-ai/Kokoro-82M-v1.0-ONNX` | **1,826 ms** to first audio, 1,831 ms total for 3.9 s of audio | (same container) |
| Voice | **Pocket TTS** (`pocket-tts`, voice `alba`) | **212 ms** to first audio, 2,203 ms total for 3.4 s of audio | 1.1 GB |
| Language model, later turns | `qwen2.5:1.5b`, 714-token agent prompt already cached | **~300 ms** to first token (276–336 ms), 14.5 tokens/s | 2.2 GB |
| Language model, first reply of a call, not warmed | same, the voice service's real first request (965 tokens: prompt, tool definitions, greeting, caller's words) | **10.6 s** to first token (10.5–10.6 s) | (same) |
| Language model, first reply of a call, warmed by ATLAS | same request, after ATLAS's [warm-up](#warming-the-prompt) | **0.35 s** to first token (0.33–0.60 s) | (same) |

An earlier run the same day measured 6.1 s for a 714-token prompt without tool
definitions (about 117 tokens/s). The 10.6 s request is larger, because it carries
everything the voice service really sends, and was read at 90–100 tokens/s.

What this means for one reply on this CPU, after the caller stops talking:

| Part | With Pocket TTS | With Kokoro |
|---|---|---|
| Speech-to-text, for a 5 s sentence | 2.0 s | 2.0 s |
| Language model to its first sentence (≈10 tokens), later turns | 1.0 s | 1.0 s |
| Voice to first audio | 0.2 s | 1.8 s |
| **Reply starts after** | **≈ 3.2 s** | **≈ 4.8 s** |

The first reply of a call, with Pocket TTS:

| Part | Not warmed | Warmed (warm-up finished before the caller spoke) |
|---|---|---|
| Speech-to-text, for a 5 s sentence | 2.0 s | 2.0 s |
| Language model to its first sentence | 11.3 s (10.6 s reading the prompt, 0.7 s for 10 tokens) | 1.0 s |
| Voice to first audio | 0.2 s | 0.2 s |
| **Reply starts after** | **≈ 13.5 s** | **≈ 3.2 s**, like any later turn |

Three findings:

- **Pocket TTS is about 8.6× faster to first audio than Kokoro here** (212 vs 1,826 ms).
  Kokoro on this server sends a short sentence only once it is fully generated; Pocket TTS
  streams frame by frame. Use Pocket TTS for English agents.
- **The first reply of a call is slow on CPU unless the prompt was warmed.** The language
  model has to read the whole first request once (10.6 s here); afterwards it is cached
  and each turn only reads the new words (~0.3 s). ATLAS now sends the prompt to a
  self-hosted model while the phone rings, which brings the first reply down to 0.35 s
  when the warm-up finishes before the caller speaks (see "Warming the prompt" below).
  Keep prompts short anyway: the warm-up takes as long as the reading it saves (the flow
  editor's token meter warns above 1,500 tokens).
- **Speech-to-text is now the largest delay.** Self-hosted Whisper waits for the caller to
  finish and then transcribes everything (0.4× real time). Streaming speech-to-text
  transcribes while the caller is still talking, which is why hosted stacks answer in
  under a second. For English, the self-hosted live server has the text about 0.2 s after
  the turn ends; see [Live transcription (streaming)](#live-transcription-streaming).

A faster CPU, a GPU or hosted speech-to-text changes these numbers a lot; re-run the
benchmark on the machine you deploy to.

### A whole turn, with all three servers busy

The figures above were measured one server at a time. In a call they share the CPU: the
voice starts speaking the first sentence while the language model is still writing the
rest of the reply. Two browser test calls on 2026-10-19 (Whisper base, `qwen2.5:1.5b`
warmed, Pocket TTS; 10 caller turns after the greeting) answered after **5.6 s** (median,
3.6–8.0 s). Where that time went, from each call's per-turn events
(`docs/perf/local-turn-contention.json`, `task1_real_call_timeline`):

| From the end of the caller's speech | Median (range) |
|---|---|
| Speech-to-text, including the 0.2 s that ends the turn | 1.8 s (1.5–2.1) |
| Waiting for the end of the turn | 0.004 s (0.002–0.015) |
| Language model to its first token | 0.9 s (0.5–1.9) |
| **Language model writing its first sentence** | **1.7 s (0.4–3.7)** |
| Voice until playback starts | 0.7 s (0.55–0.86) |

The "missing" 1.4–2 s is the language model writing its first sentence: the voice service
sends text to the voice only at a sentence end, and the first-token time stops at the first
word. In these calls the model wrote only 4–6 tokens a second. The voice also starts later
than Pocket TTS's own 0.2 s, because the voice service plays a reply only once 0.5 s of
audio has arrived.

**Thread caps.** `infra/scripts/bench_local_turn.py turn` reproduces a turn with all three
servers running: Whisper transcribes a 3 s sentence, the transcript goes to the model with
a 950-token warmed prompt, and each finished sentence goes to Pocket TTS while the model
keeps writing. It also replays the audio in real time to find gaps. 10 turns per setting,
fresh containers, no call running:

| Threads (model / Pocket TTS / Whisper) | Reply starts (median, 90th pct) | Stutter inside sentences | Pause between sentences |
|---|---|---|---|
| 6 / 1 / 4 (old default: all cores) | 3.5 s, 5.0 s | none | in 9 of 10 replies, median 0.55 s, up to 3.5 s |
| **4 / 1 / 4 (new default)** | **3.0 s, 3.6 s** | **none** | in 3 of 10 replies, at most 0.17 s |
| 5 / 1 / 4 | 3.0 s, 4.3 s | 8 times in 4 replies | in 8 of 10, up to 3.0 s |
| 3 / 1 / 4 | 3.1 s, 3.6 s | 7 times in 2 replies | in 6 of 10, up to 1.8 s |
| 4 / 2 / 4 | 3.2 s, 3.9 s | 26 times, every reply | in 2 of 10 |
| 6 / 2 / 4 (5 replies) | 5.2 s, 6.1 s | 17 times, every reply | every reply |

With all six cores the model and the voice slow each other down: the model wrote 6 tokens
a second while the voice spoke (10 before), so the second sentence often came late. Leaving
two cores to Pocket TTS (the model on 4, Pocket TTS on its default single thread plus its
decoder thread) gave the fastest and smoothest replies. Giving Pocket TTS a second thread
made it slower than real time under load (choppy audio). The compose file now sets these
defaults (`LOCAL_LLM_THREADS=4`, `LOCAL_TTS_THREADS=1`, `LOCAL_STT_THREADS=4`); on another
machine set `LOCAL_LLM_THREADS` to the number of physical cores minus 2.

**Waiting for the end of the turn.** The calls above used a Custom response speed with
"On any speech", and the turn ended within 15 ms of Whisper's transcript. With **Snappy
or Balanced, every Whisper reply waits 0.6 s more**: those presets start the caller's turn
only when words arrive, and for Whisper the words arrive all at once after the caller
stopped, which restarts the voice service's 0.6 s pause timer (measured with the voice
service's own turn-taking code). "On any speech" avoids the wait, but then any sound,
including the agent's own voice from a speaker, interrupts the agent; in the browser calls
about half the replies were cut off. Streaming speech-to-text has neither problem, so the
presets are unchanged.

**The first reply of a call.** In both browser calls the caller's first words lasted more
than 2.5 s, so the voice service's answering-machine check asked the language model,
which did not answer within its 3 s budget on this CPU; the greeting started 3 s late
(first reply 7.5–7.8 s). A first "Hello?" shorter than 2.5 s skips the check; turning
off answering-machine detection does too.

**Memory.** With the voice service, the three model servers, the dev servers and a
browser on this 12 GB machine, Windows ran out of free memory and paged the Docker
virtual machine: the model then wrote 0.3–1 tokens a second and a test call took 12–37 s
per reply. Restarting `local-llm` brought it back to 9–14 tokens a second. Keep at least
1.5 GB free (close other programs, stop `local-stt` if unused), or use a machine with
16 GB.

What one reply costs now, after the first reply, for a 3 s sentence with Whisper and
Pocket TTS: **3.0 s** in the benchmark (3.5 s before the thread caps), and about 3.4–3.6 s
in a call, where the voice service's 0.5 s of audio buffering and its own work come on
top. Not yet confirmed by a real call with the new settings. Add 0.6 s with Snappy or
Balanced.

### Warming the prompt

When an outbound call or a browser test call starts and the agent's language model is
self-hosted, ATLAS sends the agent's prompt to the model server in the background. The
server keeps what it has read and reuses the longest matching start of the next request,
so the first reply of the call only has to read the caller's words. The voice service
sends its first request after the caller's first words, so the ringing and the greeting
are otherwise idle time.

**Why it matches.** The voice service's first request is: the system text (global rules,
a blank line, then the Start prompt, each with the call's variables filled in), one tool
per path leaving the Start, the greeting once it has been spoken, then the caller's
words. The default model's chat template writes the tool list right after the system
text, in the same block. ATLAS builds the same text from the published agent and the
same variables, asks for one token and stops. Checked against the voice service's own
code for 36 calls across 3 agents: system text, greeting and tools were identical every
time; the shared start covered 98% of the real request (948 of 965 tokens).

Measured on the machine above with the voice service's exact first request (965 tokens),
3 calls each, every call to a different lead so nothing was cached by accident:

| Situation | First token of the first reply |
|---|---|
| Not warmed | **10.6 s** (10.5–10.6 s) |
| Warm-up finished, then the caller spoke | **0.35 s** (0.33–0.60 s) |
| Warm-up finished, then another prompt ran first (the voice service's answering-machine check) | **0.38 s** (0.29–0.77 s) |
| Warm-up without the tool list | 2.6 s (2.2–2.7 s) |
| Caller spoke 3 s after the warm-up started | 7.4 s (6.8–7.6 s) |
| Warm-up abandoned by ATLAS after 2 s | 5.2 s (5.0–5.3 s) |

What the numbers mean:

- **The warm-up costs as long as the reading it saves** (9.6 s for this request here). On
  an outbound call the phone rings and the greeting plays first, which usually takes
  longer. If the caller speaks sooner, the reply still gains what was already read.
- **A request in between does not undo it.** The model server keeps earlier prompts in RAM
  (`LLAMA_ARG_CACHE_RAM`, below) and switches back to the better match.
- **Without a warm-up, a prompt is read once and then reused across calls.** For an agent
  whose prompt has no lead details, the second call answered in 0.85 s and the first in
  3.0 s (312-token request). The warm-up matters most for the first call after the model
  server starts, after you edit and publish an agent, for prompts with lead details such
  as `{{full_name}}`, and for browser test calls.

When it helps less:

- **The Start uses actions or documents.** Their tool definitions are stored in the voice
  service, so ATLAS warms the text only (2.6 s instead of 0.35 s in the test above).
- **Variables only the voice service fills, or the current time.** `{{current_time}}`
  changes every second, so the match stops there. Put such variables at the end of the
  prompt.
- **The caller talks over the greeting.** The spoken part differs, so the match stops
  after the system text and tools.
- **Several calls at once.** The server reads one prompt at a time, and a queue of
  warm-ups would delay real replies, so ATLAS sends one warm-up per model server at a
  time (others are skipped) and the same prompt only once per 2 minutes.

Only self-hosted models are warmed: the "Self-hosted models" provider, or an OpenAI
connection whose base URL is a local or private address or a bare container name. The
warm-up starts after the voice service has accepted the call, runs in the background,
and never delays or fails a call; each one writes an `llm_warmup` log line with its
outcome (`sent`, `duplicate`, `busy`, `blocked`, `unreachable`, `timeout`).

Settings, in the API's environment:

| Setting | Default | Meaning |
|---|---|---|
| `ATLAS_LOCAL_LLM_WARMUP_ENABLED` | `true` | Turns the warm-up off. |
| `ATLAS_LOCAL_LLM_WARMUP_URL` | not set (`.env.example`: `http://127.0.0.1:11434/v1`) | Where ATLAS itself reaches the model server. The connection's base URL `http://local-llm:11434/v1` only resolves inside Docker, so an API running on the host needs this. When set, it is used for every self-hosted model. Outside local development it must use https (destination policy). |
| `ATLAS_LOCAL_LLM_WARMUP_TIMEOUT_SECONDS` | `30` | How long ATLAS waits for the warm-up. Keep it above the prompt reading time: abandoned at 2 s, the first reply above still took 5.2 s. |
| `ATLAS_ALLOW_PRIVATE_MODEL_URLS` | `false` | Without the URL above, ATLAS warms a connection's own base URL only if it resolves to a public address, like every customer-supplied URL. Set `true` when the model servers are on your own private network. |

Method and raw results: `docs/perf/local-llm-warmup.json`.

### Measuring on your machine

To measure on your machine:

```bash
make local-ai-up && make local-tts-up && make local-ai-models
make local-ai-bench     # Windows: .\make.ps1 local-ai-bench
# or: cd services/api && uv run python ../../infra/scripts/bench_local_models.py --json
make local-ai-down
```

What the benchmark (`infra/scripts/bench_local_models.py`) measures, one warm-up and then
the median of 3 runs:

| Stage | Measurement | Request |
|---|---|---|
| Speech-to-text | Time to transcribe 5 s of speech (made by Kokoro, or `--stt-wav file.wav`) | `POST /v1/audio/transcriptions` |
| Voice (Kokoro and Pocket TTS) | Time to first audio byte, and total, for a 12-word sentence streamed as 24 kHz PCM | `POST /v1/audio/speech`, as the voice service calls it |
| Language model | Time to first token, and tokens per second, for about 60 tokens | `POST /v1/chat/completions` with `stream: true` |

To measure whole turns with all three servers busy (stutter and pauses included), run
`infra/scripts/bench_local_turn.py` from `services/api` (`wavs`, then `turn`; `--help`
explains each step). Stop any test call first: both slow each other down.

Choose other models with flags, e.g. `--llm-model llama3.2:1b --stt-model
Systran/faster-whisper-small`; skip a target with `--skip pocket`. Targets that are not
running show as *unavailable*.

### Reading the result

For one reply the caller waits for roughly **STT total + LLM time to first sentence + TTS
time to first audio**, after the 0.2 s of silence that ends their turn. Speech-to-text
here is not streaming: it starts only when the caller stops speaking. The language model
streams, and the voice starts on the first complete sentence, so first-token time plus
about one sentence of tokens counts, not the full reply.

## Live transcription (streaming)

Whisper cannot transcribe while the caller is still talking. It waits for the end of the
utterance and then reads all of it: 2.0 s for a 5 s sentence with Whisper base on the
6-core PC, about 4 s per utterance with Whisper small.en on the 8-vCPU cloud server. The
optional `local-stt` server works **while the caller speaks**: it sends live words as they
form and has the final text ready a fraction of a second after the caller stops.

It speaks the same live protocol as Deepgram, which the voice service already supports,
so it is added as a **Deepgram connection whose base URL points at your own server**. No
audio leaves your network during calls.

### Which model, and why

The server runs **NVIDIA Parakeet TDT-CTC 110M** (`STT_MODEL=parakeet-110m`, CC BY 4.0) by
default. It was chosen on recordings of real calls, not on clean test speech.

The first version used a streaming Zipformer trained only on LibriSpeech (read
audiobooks). It scored 2.6 % word error rate on clean synthetic speech, but on the first
real test calls (a South Asian English speaker, a laptop microphone in a browser, WebRTC
through the TURN relay) it heard "Hello, can you hear me?" as "Hell opening hear me" and
much of the rest as nonsense. As a stopgap the demo organisation was switched to Whisper
small.en, which is slow on CPU (about 4 s per utterance) and, as it turned out, invents
whole sentences from background talk.

How the candidates were compared (2026-10-19/20, Oracle Cloud VM.Standard.E2.4: 8 vCPU AMD
EPYC 7551, 32 GB RAM, no GPU; raw numbers in `docs/perf/local-stt-real-voice.json`):

- **The caller's own voice.** The caller's side of the person's 6 browser test calls (the
  voice service records it as a separate track), cut into 55 utterances with a voice
  activity detector: 109 s of speech. Reference text: Whisper large-v3 (beam 5, offline,
  about 50 s per utterance on this CPU), checked against what the voice service had heard
  at the time. Only **18 utterances (76 words) were clear English**; the other 37 were
  side talk in Urdu or Hindi, mumbling or background voices, which large-v3 itself could
  not read with confidence. WER below is on the clear ones; for the rest the table counts
  how many words each model made up.
- **The automated test caller.** The 4 fixed lines of `e2e_test_call.py` (synthetic
  speech) as received by the voice service over WebRTC in 7 recorded calls: 28 utterances,
  259 words, exact text known (a few recordings lost words in transit, which counts
  against every model alike).
- Every utterance at 16 kHz (browser calls) and at 8 kHz after G.711 μ-law (phone line).
  "Final" is the time the model needs once the caller stops: flushing the last chunk for
  streaming models, transcribing the whole utterance for the others. CPU is seconds of
  CPU time per second of speech, 2 threads.

| Model (licence) | Kind | Caller's clear English, 16 kHz / 8 kHz | Words made up on the 37 unclear utterances | Test caller, 16 kHz / 8 kHz | Final, median (p90) | CPU | Memory |
|---|---|---|---|---|---|---|---|
| **Parakeet TDT-CTC 110M** (CC BY 4.0) | whole utterance | **18.4 % / 23.7 %** | 144; through the server, which drops unsure text: 23 (16 kHz), 42 (8 kHz) | **9.3 % / 10.8 %** | 254 ms (286) | 0.08 | 365 MB |
| Nemotron Speech Streaming 0.6B, 560 ms (NVIDIA Open Model License) | streaming | **18.4 % / 19.7 %** | 6 | 12.0 % / 13.1 % | 0 or 470 ms, by where the last chunk ends (468) | 0.80 | 980 MB |
| Nemotron Speech Streaming 0.6B, 160 ms | streaming | 19.7 % / 25.0 % | 11 | 12.4 % / 14.3 % | 356 ms (710) | 2.25: slower than real time | 990 MB |
| Parakeet TDT 0.6B v2 (CC BY 4.0) | whole utterance | 25.0 % / 28.9 % | 100 | 10.8 % / 11.6 % | 1,082 ms (1,200) | 0.33 | 1.1 GB |
| FastConformer hybrid large PC 114M (CC BY 4.0) | whole utterance | 25.0 % / 36.8 % | 134 | 11.6 % / 11.6 % | 263 ms (303) | 0.08 | 360 MB |
| NeMo streaming FastConformer 114M, 80 ms (CC BY 4.0) | streaming | 28.9 % / 35.5 % | 19 | 15.4 % / 16.6 % | 79 ms (155) | 0.50 | 270 MB |
| NeMo streaming FastConformer 114M, 480 ms | streaming | 34.2 % / 43.4 % | 8 | 13.9 % / 15.4 % | 0 or 105 ms | 0.18 | 270 MB |
| NeMo streaming FastConformer 114M, 1040 ms | streaming | 36.8 % / 38.2 % | 16 | 13.5 % / 14.7 % | 0 or 150 ms | 0.16 | 270 MB |
| Moonshine base, 2026 export (MIT) | whole utterance | 42.1 % / 34.2 % | 120 | 14.7 % / 17.0 % | 229 ms (298) | 0.08 | 290 MB |
| Moonshine tiny, 2026 export (MIT) | whole utterance | 85.5 % / 84.2 % | 244 | 18.5 % / 21.6 % | 133 ms (156) | 0.04 | 160 MB |
| Zipformer EN 2023-06-26 (Apache 2.0), the first model | streaming | 57.9 % / 65.8 % | 90 | 21.2 % / 27.4 % | 34 ms (44) | 0.11 | 180 MB |
| Whisper small.en via the speech server (MIT), the stopgap | whole utterance | 65.8 % / 197 % | 485 | 10.4 % / 14.3 % | 4,300 ms (5,102) | 1.4 s per s (wall time, 4 threads) | in `local-speech` |

Word error rates above 100 % mean the model wrote more wrong words than the caller said
(Whisper repeated invented phrases, for example "Very well. Very well. …"). "Final" here is
measured on the test caller's lines; Parakeet's 4-thread final was 217 ms (255), Nemotron's
329 ms (p90) with 4 threads but still 0.52 s of CPU per second of speech.

What the table says:

- **Parakeet 110M and Nemotron 0.6B are the two that understand this caller**, with about
  one word in five wrong against more than one in two for the Zipformer and Whisper
  small.en. They tie at 16 kHz (14 of 76 words each); on phone audio Nemotron was 3 words
  better. Parakeet is the most accurate on the test caller's lines, gives the final text
  about 0.2 s sooner through the voice service (below), and needs a tenth of the CPU and
  a third of the memory. Nemotron is truly streaming, but on this CPU it spends 0.8 s of
  CPU per second of speech on every call, on the cores the language model and the voice
  also need; Parakeet re-reads the utterance a few times while the caller speaks and
  spends 0.08.
- **The made-up words matter as much as WER.** When the caller's line carries side talk
  in another language or background voices, Whisper small.en writes whole sentences
  ("Assalamu alaikum…", "We'll see you next week.") that reach the agent as if the caller
  said them; Zipformer writes nonsense. Nemotron stays silent. Parakeet writes short wrong
  phrases, but with a low confidence: the server drops text whose mean token probability
  is under 0.6 (`STT_MIN_CONFIDENCE`). On these recordings the test caller's lines all
  scored 0.71 or more and the caller's clear English 0.72 or more, except one utterance
  Parakeet had wrong anyway (0.50); 24 of the 34 unclear utterances it wrote words for
  scored under 0.6.
- Whisper large-v3-turbo was not tried live: it has the same 32-layer encoder as large-v3,
  which took about 50 s per utterance here.
- **How sure is this?** Eighteen utterances from one speaker are thin evidence, and the
  reference is itself a model: on two utterances large-v3 wrote only "Hello." and "Yes."
  where other models heard more, and some clear ones it may have wrong ("Where is the gun
  going?" for a line the other models heard as some form of "what is going on"). The
  test caller's lines back up the ranking with exact references. Re-check with your own
  callers; `infra/scripts/bench_streaming_stt.py --manifest` runs any recordings through
  the server the way a call does.
- **End to end.** With the demo organisation switched to the live server, two automated
  test calls passed and the voice service heard every caller line correctly; its own
  speech-to-text measure was 324 to 580 ms per turn (Whisper small.en: about 4 s). A third
  call used four of the caller's recorded utterances as the caller's lines: "Hello, can
  you hear me?", "What is your name?" and "Where are you calling from?" came through word
  for word (the first followed by three words the reference does not have), where the
  Zipformer had heard "Hell opening hear me".

### Measured through the voice service

The test fed the recordings in 20 ms frames in real time through the voice service's own
Deepgram client, built with the same settings a call uses; the voice service ends a turn
0.2 s after the caller goes quiet and sends Finalize at that moment, and the test did the
same. Same server as above, `STT_NUM_THREADS=2`.

| Through the voice service's client | Parakeet 110M, 16 kHz | Parakeet 110M, 8 kHz phone | Nemotron 0.6B, 16 kHz | Nemotron 0.6B, 8 kHz |
|---|---|---|---|---|
| **Final transcript after the caller stops**, test caller (28 lines), median (p90) | **421 ms** (534) | **436 ms** (610) | 624 ms (807) | 614 ms (698) |
| of which after Finalize (the rest is the 0.2 s that ends the turn) | 208 ms | 221 ms | 413 ms | 404 ms |
| Same, the caller's clear English (17 utterances), median (p90) | 309 ms (571) | 405 ms (546) | 602 ms (736) | 653 ms (702) |
| First live words after the caller starts, test caller / caller | 646 / 1,096 ms | 516 / 940 ms | 1,293 / 1,550 ms | 1,140 / 1,672 ms |
| Word error rate, test caller | **10.4 %** | **10.8 %** | 17.8 % | 17.8 % |
| Word error rate, the caller's clear English (73 words) | 28.8 % | 26.0 % | 26.0 % | 19.2 % |
| Words made up on the caller's 31 unclear utterances | 23 | 42 | 9 | 6 |
| CPU peak / memory peak over the runs | 2.4 cores (two calls at once) / 346 MiB | | 2.5 cores (one call) / 978 MiB | |

More from the same runs, with Parakeet:

- **Two calls finishing at the same moment** (10 lines, both calls at once): final 495 ms
  after the caller stops (p90 585) at 16 kHz, 489 ms (p90 767) at 8 kHz; every final was
  delivered and marked finalized. CPU peaked at 2.45 cores, memory at 346 MiB.
- **48 kHz input** (10 lines): word error rate 4.2 %, final 406 ms after the caller stops.
- The live path is less accurate on the caller than decoding each recorded utterance
  whole (28.8 % against 18.4 % at 16 kHz): the server cuts at pauses, so a long
  utterance with a pause becomes two pieces, and the second piece sometimes picks up
  quiet speech the reference left out. The test caller's lines, spoken without pauses,
  came out the same (10.4 % against 9.3 %).
- Nemotron did worse through the server than on whole recordings (17.8 % against 12.0 %
  on the test caller): flushed with 0.2 s of silence on Finalize, its last 560 ms chunk
  was cut short and most lines lost their last word ("doll" for "dollars"). The server now
  pads Nemotron with 0.6 s, which costs one more chunk on every final (about 0.47 s on 2
  threads here); that setting was not re-measured.

The first version of this server (Zipformer) delivered the final 0.30 s after the caller
stopped on clean synthetic speech, 0.1 s sooner than Parakeet; on the caller's speech it
was wrong on more than half the words.

### How the server does it

- A **voice activity detector** (Silero VAD) watches the audio. When speech starts, the
  audio from 0.6 s before is kept, so the first word is not cut off; between utterances
  the model does no work. Phone audio is brought up to 16 kHz once as it arrives
  (windowed-sinc filter). The Silero model in sherpa-onnx misses much speech on narrowband
  audio (23 of 55 utterances never detected at 8 kHz, 0 at 16 kHz), so for phone calls a
  loudness detector (15 dB above the background, above −50 dBFS) also counts as speech.
- **Live words**: while the caller speaks, the utterance so far is transcribed again every
  0.4 s, and never more often than its own cost allows (at most about 40 % of a thread).
- **The final text** is started as soon as the detector hears the pause, about 0.1 s
  before Finalize arrives, so the voice service gets it 0.1 to 0.2 s after Finalize.
- Text the model is unsure of is dropped (above). An utterance longer than 20 s is cut
  and finalised anyway.

### Why not stream Whisper

Whisper has no streaming mode. Projects that make it look live re-transcribe a growing
window of audio every fraction of a second. On these CPUs that costs more than it saves:

- Whisper's encoder always processes a fixed 30-second window, so each re-run costs about
  as much as a full transcription: 2.0 s with base on the 6-core PC, about 4 s with
  small.en on the cloud server, measured.
- Updating every 0.5 s during a 5 s sentence means 10 re-runs: 20 to 40 s of CPU work for
  5 s of speech. It cannot keep up in real time, and it takes the cores the language model
  needs.

Parakeet's encoder only reads the audio there is, so re-reading a 3 s utterance costs
about 0.2 s on 2 threads. **Whisper stays the choice for Urdu and other languages**; the
live server is English only.

### Set it up

```bash
make local-stt-up       # Windows: .\make.ps1 local-stt-up
curl http://127.0.0.1:8003/health
```

Then in **Voice AI → AI providers**:

1. **Add connection → Deepgram**, name it "Live speech", base URL `http://local-stt:8000`.
2. Under **Speed**, apply the **Private streaming (self-hosted)** preset. It needs this
   connection, the self-hosted language model connection and the Pocket TTS connection.
   To choose by hand instead: Speech-to-text *Live speech*, model `parakeet-en-110m`,
   language `en`. The server runs whatever `STT_MODEL` says; the model name in ATLAS only
   has to avoid the two Flux names (below).

**The API key is checked with Deepgram's cloud when the connection is saved.** The pinned
voice service verifies every Deepgram key against `api.deepgram.com` when a connection is
created or its base URL changes, and ignores the base URL for that check. A made-up key is
therefore refused (verified), so you need a valid Deepgram key once, for example from a
free account. The streaming server ignores the key. During calls the key is only sent to
your server, together with the audio. Until the voice service skips this check for custom
endpoints, this is the one step that contacts a third party.

The model name only has to avoid the two Flux names (`flux-general-en`,
`flux-general-multi`): those make the voice service use a different protocol that this
server does not speak. ATLAS never picks the streaming connection for the cloud presets.

Re-deploying never changes an organisation's speech-to-text: `deploy.sh` and the demo
setup apply a speed preset only on the first setup, or when you pass `--preset`
(`-Preset` on Windows). An organisation that used the old Zipformer keeps its connection
and model name; the server answers with the model it runs now.

### Other models

Set `LOCAL_STT_MODEL` in `.env` (read by `docker-compose.yml` as `STT_MODEL`) and restart
`local-stt`; the model downloads on first start.

| `STT_MODEL` | Download | Memory limit to set | Notes |
|---|---|---|---|
| `parakeet-110m` (default) | 108 MB archive (sherpa-onnx release, SHA-256 pinned) | 768 MB (default; 346 MiB measured with two calls) | as measured above |
| `nemotron-en-560` | 660 MB | 1.5 GB | truly streaming, as accurate on the caller; needs a faster CPU (0.78 s CPU per s of speech here) |
| `nemotron-en-160` | 660 MB | 1.5 GB | slower than real time on this CPU |
| `parakeet-tdt-v2` | 660 MB | 1.5 GB | about 0.6–1.2 s per utterance here; for a GPU or a fast CPU |
| `zipformer-en` | 75 MB | 512 MB | the first model; not recommended (above) |

### Licensing, checked at the source

- Default model: **Parakeet TDT-CTC 110M**, by NVIDIA, **CC BY 4.0**, which allows
  commercial use with attribution
  ([nvidia/parakeet-tdt_ctc-110m](https://huggingface.co/nvidia/parakeet-tdt_ctc-110m);
  [licence](https://creativecommons.org/licenses/by/4.0/)). Trained on about 36,000 hours
  of English. The server uses the int8 ONNX export published by the sherpa-onnx
  maintainers
  ([release asset](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-nemo-parakeet_tdt_transducer_110m-en-36000-int8.tar.bz2),
  export script `scripts/nemo/fast-conformer-hybrid-transducer-ctc/run-transducer-non-streaming.sh`
  in [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)). The archive's SHA-256
  (`f628312e…`) is pinned and matches the digest GitHub publishes; only the four model
  files are kept, each checked by SHA-256. Attribution is in `THIRD_PARTY_NOTICES.md`.
- Voice activity detector: Silero VAD, **MIT**
  ([snakers4/silero-vad](https://github.com/snakers4/silero-vad)), the copy published in
  the sherpa-onnx release, SHA-256 pinned.
- The alternatives, checked the same way:
  - Nemotron Speech Streaming EN 0.6B: **NVIDIA Open Model License**, "ready for
    commercial/non-commercial use" on the model card
    ([nvidia/nemotron-speech-streaming-en-0.6b](https://huggingface.co/nvidia/nemotron-speech-streaming-en-0.6b),
    [agreement](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-open-model-license/)).
    Redistributing it requires the notice "Licensed by NVIDIA Corporation under the NVIDIA
    Open Model License"; the licence ends if its guardrails are bypassed or if you sue
    over it. Files from
    [csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25](https://huggingface.co/csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-int8-2026-04-25)
    at a fixed commit.
  - Parakeet TDT 0.6B v2: **CC BY 4.0**
    ([nvidia/parakeet-tdt-0.6b-v2](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v2)).
  - NeMo streaming FastConformer 80/480/1040 ms and FastConformer hybrid large PC:
    **CC BY 4.0** ([nvidia/stt_en_fastconformer_hybrid_large_streaming_multi](https://huggingface.co/nvidia/stt_en_fastconformer_hybrid_large_streaming_multi)).
    Measured only, not offered in the server.
  - Moonshine English models: **MIT** (the LICENSE in each sherpa-onnx archive and
    [moonshine-ai/moonshine](https://github.com/moonshine-ai/moonshine); models for other
    languages are under a non-commercial community licence). Measured only.
  - Zipformer EN 2023-06-26: **Apache 2.0**
    ([csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26)),
    trained on LibriSpeech (CC BY 4.0).
  - Not used: Nemotron 3.5 ASR Streaming 0.6B (OpenMDW-1.1, commercial use allowed)
    adds 40 languages but not Urdu, and is the same size as Nemotron, which is already
    too slow here; Parakeet unified 0.6B (NVIDIA Open Model License) likewise.
- Software: sherpa-onnx (Apache 2.0, [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)),
  which bundles ONNX Runtime (MIT); websockets (BSD 3-Clause); NumPy (BSD 3-Clause).
- Still not used, because of their licences: the LibriSpeech + GigaSpeech Zipformer
  (GigaSpeech audio is for non-commercial research,
  [speechcolab/gigaspeech](https://huggingface.co/datasets/speechcolab/gigaspeech)) and the
  Kroko models (CC BY-SA with a separate commercial licence,
  [Banafo/Kroko-ASR](https://huggingface.co/Banafo/Kroko-ASR)).

### Languages

English only. For Urdu or mixed-language calls keep Whisper (`Systran/faster-whisper-small`
or larger) on the speech server. Urdu side talk on an English call is dropped by the
confidence filter rather than transcribed.

### What the server supports

| Part of the protocol | Support |
|---|---|
| `wss/ws …/v1/listen`, 16-bit PCM (`encoding=linear16`), mono, any `sample_rate` from 8,000 to 48,000 Hz | Yes; audio is resampled to 16 kHz as it arrives |
| Interim results (`interim_results=true`) | Yes, whenever the recognised words change (the utterance is re-read every 0.4 s or less often) |
| Final results at a pause (`is_final`, `speech_final`) | Yes, after `STT_ENDPOINT_SILENCE_MS` of silence; `endpointing=false` turns this off |
| `Finalize` → final result with `from_finalize: true` | Yes, immediately (an empty final if nothing was pending) |
| `KeepAlive`, `CloseStream` (→ last final, then `Metadata`) | Yes |
| `utterance_end_ms` (`UtteranceEnd`), `vad_events` (`SpeechStarted`) | Yes; `SpeechStarted` from the voice activity detector |
| `confidence` | Mean token probability of the text |
| `keyterm`, `punctuate`, `smart_format`, `diarize`, `language`, other options | Accepted and ignored (Parakeet writes punctuation and capitals itself) |
| `Authorization` header | Accepted as is, unless `STT_API_KEY` is set |
| Other encodings (μ-law, Opus), multichannel | Refused with HTTP 400 (the voice service sends linear16) |
| Flux (`/v2/listen`) | No |

### Streaming server settings

| Variable | Default | Meaning |
|---|---|---|
| `STT_MODEL` | `parakeet-110m` | Which model (table above); `docker-compose.yml` reads it from `LOCAL_STT_MODEL` |
| `STT_NUM_THREADS` | `2` | CPU threads per decode (`LOCAL_STT_STREAM_THREADS`). 4 threads made Parakeet's final about 15 % faster here. |
| `STT_MAX_SESSIONS` | `4` | Calls transcribed at once; more are refused (HTTP 503) and the voice service retries |
| `STT_ENDPOINT_SILENCE_MS` | `400` | Silence after words that ends a segment when no Finalize arrives first |
| `STT_MIN_CONFIDENCE` | `0.6` (Parakeet), `0` (others) | Text with a lower mean token probability is dropped |
| `STT_VAD_THRESHOLD` | `0.5` | Speech probability that counts as speech |
| `STT_PREROLL_MS` | `600` | Audio kept from before the detected start of speech |
| `STT_INTERIM_MS` | `400` | Whole-utterance models: how often live words are refreshed, at most |
| `STT_FINALIZE_PAD_MS` | `200` (Nemotron `600`, Zipformer `800`) | Streaming models: silence added on a final so the last word is processed |
| `STT_MAX_UTTERANCE_S` | `20` | A segment longer than this is finalised anyway |
| `STT_IDLE_TIMEOUT_S` | `30` | A connection that sends nothing (not even KeepAlive) for this long is closed |
| `STT_MODEL_ID` | per model | Name reported in results and `/health` |
| `STT_API_KEY` | unset | If set, clients must send `Authorization: Token <key>` (or `Bearer`) |

Transcripts are never written to the server log at the default level. The server's own
tests: `infra/docker/streaming-stt/test_app.py` (run command at the top of the file).

To measure on your machine (the voice service must be running):

```bash
make local-stt-up
# speech.wav: any 16-bit mono WAV; put its exact words in --text
docker cp speech.wav atlas-voice-runtime-1:/tmp/stt-bench/speech.wav
docker compose --profile voice exec -T voice-runtime /opt/venv/bin/python - \
  --wav /tmp/stt-bench/speech.wav --text "the words spoken" \
  < infra/scripts/bench_streaming_stt.py
# many recordings: --manifest clips.json ([{"wav": "...", "text": "..."}]), --runs 1,
# --concurrency 2 for two calls at once, --no-text to keep transcripts out of the output
```

## Pocket TTS or Kokoro

Both run on CPU, stream audio, and answer the same OpenAI-style speech request.

| | Kokoro 82M (in `local-speech`) | Pocket TTS 100M (`local-tts`) |
|---|---|---|
| Publisher | hexgrad, served by the speech server | Kyutai, served by ATLAS's small adapter |
| Weights license | Apache 2.0 (upstream `hexgrad/Kokoro-82M`; the ONNX repack card states none) | CC BY 4.0: commercial use allowed, with attribution to Kyutai |
| Code license | Apache 2.0 (speech server: MIT) | MIT |
| Languages | English and several others, by voice | English, French, German, Portuguese, Italian, Spanish, Dutch (one language per server) |
| Voices | Built-in voice packs (`af_heart` …) | 27 built-in voices; ATLAS allows only those licensed for commercial use (below). Optional [voice cloning](#voice-cloning) with the gated weights |
| Streaming | Phrase-sized chunks; a short sentence arrives in one piece (measured: first audio ≈ total) | Yes, frame by frame (12.5 frames per second) |
| Measured here (i5-8500T) | 1,826 ms to first audio | 212 ms to first audio (Kyutai states ~200 ms on a MacBook Air M4) |
| Memory limit in compose | shared 1.75 GB with Whisper | 1.5 GB |

**Which to pick:** Kokoro is the default because it is already in the speech server and
needs no extra container. Try Pocket TTS when the first word of each reply comes too late,
since it is built for low time-to-first-audio on CPU and leaves cores free for the
language model. Run the benchmark to decide on your hardware. Neither model speaks Urdu.

### Pocket TTS licensing, checked at the source

- Code: MIT ([github.com/kyutai-labs/pocket-tts](https://github.com/kyutai-labs/pocket-tts)).
- Weights: CC BY 4.0. The main repository
  [kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts) is **gated** (you must
  log in and accept terms) and holds the voice-cloning weights. ATLAS uses it only when
  you turn on [voice cloning](#voice-cloning) with a token. Otherwise the library falls
  back to
  [kyutai/pocket-tts-without-voice-cloning](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning),
  which is **not gated**, is also CC BY 4.0, and holds the built-in voices. An anonymous
  download returns HTTP 200 there and 401 from the gated repository.
- Acceptable use (from the model card): no voice impersonation or cloning without
  consent, no deception (including fraudulent calls or presenting generated speech as a
  real person), no unlawful or harmful content. Speech requests name a built-in or a
  cloned voice id, never a URL or a file; cloning is a separate, consent-gated upload
  ([Voice cloning](#voice-cloning)) and is off unless the gated weights are set up.
- Attribution: credit "Kyutai Pocket TTS (CC BY 4.0)" and, for VCTK or Alba MacKenna
  voices, that voice's source, wherever you list third-party components.

Per-voice licenses, from [kyutai/tts-voices](https://huggingface.co/kyutai/tts-voices)
and the voice sources in the library (v3.3.0):

| Voices | Recording source | License | Allowed by default |
|---|---|---|---|
| `alba` | Alba MacKenna | CC BY 4.0 | yes (default voice) |
| `anna`, `vera`, `fantine`, `charles`, `paul`, `eponine`, `azelma`, `george`, `mary`, `jane`, `michael`, `eve` | VCTK dataset | CC BY 4.0 | yes |
| `marius`, `javert` | Unmute voice donations | CC0 | yes |
| `bill_boerst`, `peter_yearsley`, `stuart_bell`, `caro_davy` | Voice-Zero (LibriVox) | CC0 | yes |
| `estelle` | Kyutai's own recording | CC0 | yes |
| `cosette` | Expresso dataset | CC BY-NC 4.0, **non-commercial** | no |
| `jean` | EARS dataset | CC BY-NC 4.0, **non-commercial** | no |
| `giovanni`, `lola`, `juergen`, `rafael`, `daan` | not documented by Kyutai | unverified | no |

Change the list with `POCKET_TTS_VOICES` (comma-separated) on the `local-tts` service. The
server logs a warning if you add a voice that is not cleared for commercial use.
`GET http://127.0.0.1:8002/v1/audio/voices` lists the active voices and their licenses.

### Pocket TTS server settings

| Variable | Default | Meaning |
|---|---|---|
| `POCKET_TTS_LANGUAGE` | `english` | Model to load: `english`, `french`, `german`, `portuguese`, `italian`, `spanish`, `dutch` (and `_24l` variants: better, slower) |
| `POCKET_TTS_MODEL_ID` | `pocket-tts` | The `model` value clients must send |
| `POCKET_TTS_DEFAULT_VOICE` | `alba` | Used when a client sends an OpenAI voice name such as `alloy` |
| `POCKET_TTS_VOICES` | commercial-use voices | Allow-list of voice names |
| `POCKET_TTS_API_KEY` | unset | If set, clients must send `Authorization: Bearer <key>` |
| `POCKET_TTS_WARMUP` | `1` | Synthesises one short phrase at startup, so the first call is not slow |
| `POCKET_TTS_THREADS` | `1` in compose (`LOCAL_TTS_THREADS`); unset = the library's own choice, also 1 | CPU threads for synthesis; the audio decoder runs on one more thread. Alone, 2 threads reach first audio a little sooner (about 150 vs 180 ms), but while the language model runs they made the voice slower than real time: 26 stutters in 10 replies, against none with 1 |
| `HF_TOKEN` | compose passes `POCKET_TTS_HF_TOKEN` from `.env`; empty | Hugging Face token of an account that accepted the gated weights' terms: turns on [voice cloning](#voice-cloning). Never logged |
| `POCKET_TTS_VOICE_DIR` | `/home/app/.cache/voices` (in the weights volume) | Where cloned voices are kept: `<id>.safetensors` (the computed voice) and `<id>.json` (name, date, sample length, consent statement) |
| `POCKET_TTS_MAX_CUSTOM_VOICES` | `50` | Most cloned voices kept on the server (each 6 to 19 MB on disk) |
| `POCKET_TTS_CUSTOM_VOICE_CACHE` | `4` | Cloned voices held in memory, least recently used dropped first (the newest are loaded at startup) |
| `POCKET_TTS_PHRASE_CACHE` | the demo agent's 15 openers (`Got it.`, `Okay.`, `Sure.`, ...: `DEFAULT_CACHED_PHRASES` in `app.py`) | `\|`-separated phrases synthesised at startup and kept in memory; `""` = none |
| `POCKET_TTS_PHRASE_CACHE_VOICES` | `POCKET_TTS_DEFAULT_VOICE` | Voices those phrases are made for |
| `POCKET_TTS_PHRASE_CACHE_MAX_CHARS` | `32` | Other texts up to this length are kept too once fully spoken (least recently used dropped first); `0` = only the startup phrases |
| `POCKET_TTS_PHRASE_CACHE_MB` | `16` | Memory for all kept audio (about 330 s of speech) |

The adapter supports `response_format` `pcm` (24 kHz, 16-bit, mono, as the voice service
expects) and `wav`, and streams audio as it is generated. It runs one synthesis at a time,
because the model is not thread-safe. Pocket TTS has no speed control, so `speed` is
accepted and ignored.

**Phrase cache.** The voice service starts a sentence only once 0.5 s of its audio has
arrived, which takes Pocket TTS 0.44-0.52 s on a 4-core server. A kept phrase is answered
from memory (first 0.5 s in about 3 ms), starting 30 ms before its first sound instead of
80-175 ms. A sentence that begins with a kept phrase and goes on (`Got it, perfect
timing.`) is answered with the kept `Got it.` and then the synthesised rest, so the first
words still come at once; `Great!` is answered with the kept `Great.`. The response header
`X-Phrase-Cache` says `hit`, `opener` or `miss`, and `GET /v1/audio/phrase-cache` gives
counts only (never texts). Audio is kept in memory only. Measurements:
[perf/oracle-latency.md](perf/oracle-latency.md).

### Voice cloning

Pocket TTS can speak in a new voice computed from a short recording. ATLAS offers this as
**AI providers → Voice → Clone a voice** (also in an agent's **Voice & model** tab) for
Pocket TTS connections. It is **off by default** because it needs the gated weights.

**Rules (from the model card, shown in the form in plain words).** Only clone the voice of
a person who gave permission, and keep a record of it. Never use a cloned voice to deceive:
no fraudulent or scam calls, no presenting generated speech as the real person. Nothing
unlawful or harmful. Whoever clones a voice must tick "I have the speaker's permission to
clone their voice"; ATLAS refuses the request without it and writes the attestation to the
audit log (`ai.voice_cloned`: who, when, voice id and name, the attestation; never the
audio). Only owners and admins can clone or delete voices.

**Turn it on (one time).**

1. Sign in at Hugging Face and accept the terms of
   [kyutai/pocket-tts](https://huggingface.co/kyutai/pocket-tts).
2. Create a **read** token for that account at
   [huggingface.co/settings/tokens](https://huggingface.co/settings/tokens).
3. Put it in `.env` as `POCKET_TTS_HF_TOKEN=hf_…`, or let the demo setup do it:
   `.\make.ps1 demo -HfToken hf_…` or `bash infra/scripts/setup_local_demo.sh --hf-token hf_…`
   (better: set `ATLAS_DEMO_HF_TOKEN` first, so the token stays out of shell history). The
   scripts never print it. Compose passes it to `local-tts` as `HF_TOKEN`.
4. Restart the server: `docker compose --profile local-ai up -d local-tts`. It downloads
   the cloning weights once (about 0.2 GB, into the `local_tts_models` volume) and logs
   `Voice cloning enabled`; `GET http://127.0.0.1:8002/v1/voices` then answers 200.
5. When the API runs on the host (as in the demo), it reaches the server through
   `ATLAS_LOCAL_TTS_URL=http://127.0.0.1:8002/v1` (in `.env.example`; the setup scripts add
   it to an existing `.env`). The connection's own base URL, `http://local-tts:8000/v1`,
   only resolves inside Docker. If the server sets `POCKET_TTS_API_KEY`, set the same value
   as `ATLAS_LOCAL_TTS_API_KEY`.

Without a token, or when the token's account has not accepted the terms (the download is
refused and the library falls back to the open weights), the server's `/v1/voices`
endpoints answer **501** with these steps, and ATLAS shows them instead of the form.
Built-in voices keep working either way.

**Limits.** One speaker, 5 to 30 seconds after decoding, at most 10 MB. The web app
accepts WAV, MP3 and M4A files or records in the browser (a timer stops it at 30 s), and
converts the sample to a 24 kHz mono WAV before uploading. The server reads WAV only
(16/24/32-bit PCM or float, common sample rates) on purpose: it has no MP3 or AAC decoder.
A quiet room and 10 to 20 seconds of natural speech work best. One voice is computed at a
time (a second request gets 429), and the server keeps at most
`POCKET_TTS_MAX_CUSTOM_VOICES` voices.

Measured on the dev CPU (i5-8500T, one torch thread, open weights and a synthetic signal):
computing a voice takes about 6 s for 10 s of audio and 8 s for 30 s, and stays within the
memory the model already needs while loading (about 1 GB peak). The library's one-pass
encoder grows with the square of the length (27 s and 840 MiB extra for 30 s, more than the
container's 1.5 GB limit allows), so the adapter encodes the sample in windows that give
the same result (checked by `BoundedEncoderTest` in `infra/docker/pocket-tts/test_app.py`).
The sample's length does not slow calls down: first audio stayed about 150 ms with a 10, 20
or 30 s voice.

**What is stored.** The uploaded audio is decoded in memory and discarded. The server
keeps only the computed voice state (safetensors, which cannot carry code) and a small
JSON record with the voice's name, date, sample length and consent statement. ATLAS keeps
a `custom_voices` row (organization, connection, voice id, name, creator, date). One server
stores the voices of every organization, so ATLAS shows, selects and deletes only an
organization's own: ids end in 16 random hex digits (`custom-<name>-<random>`), lists are
filtered per organization, and saving another organization's voice id in the models is
refused. Deleting a voice that the organization's models or an agent still use is refused.

**Use it.** After cloning, the voice appears in the voice picker with a **Cloned** badge
and is selected; save the models (or the agent) to use it on calls. Delete it from the
same picker (trash icon, with confirmation). If the token is removed later, cloned voices
stay on disk but the server refuses to speak them until cloning is on again.

**API.** `GET /api/v1/ai/connections/{id}/voices` (managers and up),
`POST /api/v1/ai/connections/{id}/voices` (multipart `name`, `audio`,
`consent_attestation=true`) and `DELETE /api/v1/ai/connections/{id}/voices/{voice_id}`
(owners and admins). The server itself exposes `GET`/`POST /v1/voices` and
`DELETE /v1/voices/{id}`. Its tests (with a fake model) run inside the image:

```bash
docker compose --profile local-ai run --rm --no-deps \
  -v "$PWD/infra/docker/pocket-tts:/srv/tests:ro" --entrypoint python \
  local-tts -m unittest discover -s /srv/tests -t /srv/tests
```

## Every speed setting, explained

These are set in `docker-compose.yml`. Each name was checked against the source code of
the pinned image version.

**Language model (`local-llm`)**

| Setting | Value | Why |
|---|---|---|
| `OLLAMA_KEEP_ALIVE` | `-1` (override with `LOCAL_LLM_KEEP_ALIVE`) | Never unload the model. Loading it again adds seconds to the next reply. Set `30m` to give RAM back when idle. |
| `OLLAMA_NUM_PARALLEL` | `1` | One request at a time gets every CPU core. Parallel slots split cores and multiply context memory. |
| `OLLAMA_MAX_LOADED_MODELS` | `1` | Only one model in RAM. |
| `OLLAMA_CONTEXT_LENGTH` | `4096` | Phone prompts are short. A fixed window keeps memory predictable. |
| `OLLAMA_FLASH_ATTENTION` | `1` | Supported on CPU (the default is "auto"). Saves memory as context grows, and measured 6–9% faster to first token on a fresh 740-token prompt (7.8–8.1 s on, 8.6 s off). |
| `OLLAMA_NO_CLOUD` | `1` | No remote inference or web search, so everything stays local. |
| `LLAMA_ARG_CACHE_RAM` | `256` | RAM for earlier prompts the model server can switch back to (read by the llama.cpp server inside the image). Its default, 8192 MiB, is far above the container's 2.5 GB limit; a 965-token call prompt takes 27 MiB, so 256 MiB keeps about 9. |
| `LLAMA_ARG_THREADS` | `4` (override with `LOCAL_LLM_THREADS`; `-1` = all cores) | CPU threads of the model server (read by the llama.cpp server; Ollama passes no thread count unless a model sets `num_thread`, and the voice service's OpenAI-style requests cannot). Leaves two of the six cores to Pocket TTS, which speaks while the model is still writing: replies started 0.5 s sooner and without mid-reply pauses ([A whole turn](#a-whole-turn-with-all-three-servers-busy)). Use physical cores minus 2. |

Also measured on 2026-10-19 and left at their defaults (fresh 740-token prompt, configs
measured in alternation; `docs/perf/local-llm-tuning.json`):

| Option | Tried | Result |
|---|---|---|
| `num_thread` | 4, 5, 6 | Alone, 6 (all cores) is fastest: 4 is 19% and 5 is 11% slower to first token. With Pocket TTS speaking at the same time, 4 is best; see `LLAMA_ARG_THREADS` above |
| `num_batch` | 256, 512, 1024 | No consistent difference: 1024 against 512 was 8% faster in one run, then 2% slower, 2% faster and 2% slower; 512 kept |
| `num_ctx` | 2048, 4096 | 2048 within noise (4% faster in one run); 4096 kept for longer calls |
| Quantisation | `qwen2.5:1.5b` (q4_K_M), `qwen2.5:1.5b-instruct-q4_0` | q4_0 is 18% slower to first token here (83 vs 98 tokens/s reading the prompt) |
| Reference | `qwen2.5:0.5b` | Reads prompts 2.1× faster (208 tokens/s; 3.6 s to first token) but follows instructions worse |

These runs shared the CPU with a stuck background process using about 1.2 cores, so their
absolute times are slower than the warm-up measurements above; each comparison was made
within the same run.

Not set: `OLLAMA_KV_CACHE_TYPE` (for example `q8_0`) halves context memory but can cost a
little speed on CPU. Use it only if memory is the limit.

**Default language model: `qwen2.5:1.5b`.** The smallest size that still follows
instructions and calls tools reliably enough for an agent. Ollama lists it with tool
support, it is multilingual, and it is Apache 2.0. `qwen2.5:0.5b` is faster but often
ignores instructions. `llama3.2:1b` is a reasonable alternative (Llama 3.2 Community
License). Note that **`qwen2.5:3b` uses the Qwen Research License, which does not allow
commercial use**. Use `llama3.2:3b` if you need a 3B model for production.

**Speech server (`local-speech`)**

| Setting | Value | Why |
|---|---|---|
| `WHISPER__COMPUTE_TYPE` | `int8` | Quantised weights run faster on CPU, with a small accuracy cost. |
| `WHISPER__CPU_THREADS` | `4` (override with `LOCAL_STT_THREADS`) | Whisper threads. Speech-to-text runs before the language model and the voice, so they don't compete. 2 and 6 threads were also tried; timings varied too much between runs to pick another value. |
| `WHISPER__TTL` | `-1` | Never unload. In this version the same timer also applies to Kokoro. |
| `ENABLE_UI` | `false` | No web UI: less memory, faster start. |

**Default speech-to-text: `Systran/faster-whisper-base`.** It has 74M parameters,
against 244M for `small`, so it transcribes faster. The trade-off is accuracy: base makes
noticeably more mistakes with accents, noisy lines, names and numbers, and much more
outside English. For Urdu and other non-English calls, use `Systran/faster-whisper-small`
or larger (`STT_MODEL=Systran/faster-whisper-small make local-ai-models`). English-only
calls can use `Systran/faster-whisper-base.en`.

## What to expect

| Where the models run | What callers notice |
|---|---|
| This kind of CPU (4–8 cores, no GPU) | Fine for building and testing agents. Measured here: replies start about 3.2 s after the caller stops with Whisper and Pocket TTS, about 1.3 s with the streaming server (English; measured with its first model, the current one gives the text about 0.1 s later), and for the first reply of a call about the same when ATLAS's prompt warm-up finished in
time (about 13.5 s when it did not). Too slow for production calls. |
| A GPU server (for example one 24 GB GPU running an 8B model, Whisper and a voice) | Production-grade replies with fully self-hosted data. |
| Hosted providers with streaming speech-to-text and end-of-turn detection | Usually the fastest replies; audio leaves your network. |

To use a GPU server instead, run the same images with GPU support on that machine and
point the connections' base URLs at it. Nothing else changes.

The model servers are only part of the delay. Turn-taking settings decide how long the
agent waits before it starts answering. Set them per agent under **Call settings →
Response speed** (Snappy, Balanced, Patient), and check what calls actually measured on
each call's response-time panel and in Reports. The design is in
`docs/decisions/0006-low-latency-voice.md`.

## Choosing models

| Need | Option |
|---|---|
| Better reasoning | `llama3.2:3b`, or 7–8B models on a GPU (check each model's license) |
| Urdu or other languages | Multilingual Whisper `small` or larger; an LLM trained on the language |
| Fastest speech recognition | English: the live server (`local-stt`, Parakeet 110M), also the most accurate on real callers in ATLAS's tests. Other languages: `Systran/faster-whisper-base` (default) or `tiny` (much less accurate) |
| Fastest first audio on CPU | Try Pocket TTS and compare with the benchmark |

Override the defaults when downloading:

```bash
LLM_MODEL=llama3.2:1b STT_MODEL=Systran/faster-whisper-small make local-ai-models
```

## Privacy note

With self-hosted models selected, audio and text stay on your infrastructure. Pocket TTS
and the live speech server contact Hugging Face (and, for the live server's default
model, the sherpa-onnx release on GitHub) only to download their models on first start. Saving the Deepgram connection for the streaming server sends its API key (no audio)
to Deepgram once, as described above. Two
optional features of the underlying voice service contact an external service even then:
knowledge-base document parsing, and the hosted trial models (disabled by default; see
`ATLAS_VOICE_ALLOW_HOSTED_TRIAL_MODELS`). Review these before deploying in a regulated
environment.
