# Running AI models on your own hardware

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
| `local-stt` (optional) | built from `infra/docker/streaming-stt` | Live (streaming) English speech-to-text | sherpa-onnx streaming Zipformer (Apache 2.0) |

Model files are stored in Docker volumes (`local_llm_models`, `local_speech_models`,
`local_tts_models`, `local_stt_models`), which live on the Docker data disk, so nothing is
written to the system drive.

## Set up (one time)

```bash
make local-ai-up        # Windows: .\make.ps1 local-ai-up
make local-ai-models    # downloads about 1.5 GB of models
make local-tts-up       # optional: builds and starts Pocket TTS (weights ≈0.3 GB on first start)
make local-stt-up       # optional: live English transcription (model ≈75 MB on first start)
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
  under a second. For English, the self-hosted streaming server cuts this to 0.1 s; see
  [Live transcription (streaming)](#live-transcription-streaming).

A faster CPU, a GPU or hosted speech-to-text changes these numbers a lot; re-run the
benchmark on the machine you deploy to.

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
utterance and then reads all of it, which costs 2.0 s for a 5 s sentence on this CPU. The
optional `local-stt` server recognises speech **while the caller speaks**. It sends live
partial words as they form, and has the final text ready almost as soon as the caller stops.

It speaks the same live protocol as Deepgram, which the voice service already supports,
so it is added as a **Deepgram connection whose base URL points at your own server**. No
audio leaves your network during calls.

### Measured

Same machine as above (i5-8500T, 6 cores, no GPU) on 2026-10-19. The test fed speech in 20 ms
frames in real time through the voice service's own Deepgram client, built with the same
settings a call uses. The voice service ends a turn 0.2 s after the caller goes quiet and
sends Finalize at that moment, and the test did the same. 8 kHz is the phone rate; that
audio also went through the μ-law coding a phone line applies. Speech was made by Pocket TTS
(voice `alba`): two sentences (3.4 s and 5.7 s), five runs each, median shown. Raw results:
`docs/perf/local-streaming-stt.json`.

| | Phone audio (8 kHz) | 16 kHz audio |
|---|---|---|
| First live words, after the caller starts speaking | 0.78 s | 0.78 s |
| **Final transcript, after the caller stops** | **0.30 s** (0.26–0.32) | **0.28 s** (0.26–0.32) |
| of which: after Finalize (the rest is the 0.2 s silence that ends the turn) | 85 ms (52–105) | 73 ms (49–107) |
| Without Finalize (the server's own end-of-speech detection, 400 ms of silence) | 1.15–1.25 s | 1.16–1.26 s |
| Word error rate on the test sentences | 2.6 % (1 of 38 words: "Hi" heard as "Ay") | 2.6 % (same) |
| Memory (peak) | 215 MiB; 217 MiB with 4 calls at once; flat over an 8-minute call | |
| CPU | about 1.2 cores at peak while one caller speaks | |

With four calls finishing at the same moment (the worst case), the final transcript came
214 ms (median, 134–289) after Finalize. A fifth simultaneous call is refused with HTTP
503 (`STT_MAX_SESSIONS`) and the voice service retries. Eight minutes of audio were
transcribed in 49 s, ten times faster than real time.

Compared with Whisper (`Systran/faster-whisper-base`, int8): **0.30 s instead of about
2.2 s** from the caller stopping to the final text, for a 5 s sentence (Whisper: the same
0.2 s wait, then 2.0 s to transcribe). Whisper's delay
grows with the length of the sentence; the streaming server's does not, because it has
already processed everything except the last fraction of a second. The voice service marks
the result as finalized, so the turn ends without its extra safety wait.

Live words matter for more than speed. Snappy and Balanced response speeds treat the caller's
speech as an interruption only once the first words are recognised. With Whisper there are
no words until the caller stops, so a caller cannot interrupt the agent mid-sentence. With
the streaming server, the first words arrive about 0.8 s after the caller starts talking,
and the interruption is recognised as soon as the required number of words has been heard.

What one reply costs on this CPU, after the caller stops talking (later turns; the first
reply of a call adds the ~6 s prompt read described above):

| Part | Streaming + Pocket TTS | Whisper + Pocket TTS | Whisper + Kokoro |
|---|---|---|---|
| Speech-to-text, for a 5 s sentence (after the 0.2 s that ends the turn) | 0.1 s | 2.0 s | 2.0 s |
| Language model to its first sentence (≈10 tokens), later turns | 1.0 s | 1.0 s | 1.0 s |
| Voice to first audio | 0.2 s | 0.2 s | 1.8 s |
| **Reply starts after** | **≈ 1.3 s** | **≈ 3.2 s** | **≈ 4.8 s** |

Accuracy is the trade-off. The model was trained only on LibriSpeech (read audiobooks), so
it is weaker than Whisper on accents, noisy lines, names and numbers. It also has no
punctuation: the server lower-cases the text and capitalises the first word. Check it with
your own callers before relying on it.

### Why not stream Whisper

Whisper has no streaming mode. Projects that make it look live re-transcribe a growing
window of audio every fraction of a second. On this CPU that costs more than it saves:

- Whisper's encoder always processes a fixed 30-second window, so each re-run costs about
  as much as a full transcription. That is 2.0 s here, measured.
- Updating every 0.5 s during a 5 s sentence means 10 re-runs: roughly 20 s of CPU work
  for 5 s of speech. It cannot keep up in real time.
- It would also take the cores the language model needs.

A streaming transducer such as Zipformer processes each new 0.32 s chunk of audio once and
keeps its state, so its cost grows with the audio, not with how often results are read.
**Whisper stays the choice for Urdu and other languages**; the streaming server is English
only.

### Set it up

```bash
make local-stt-up       # Windows: .\make.ps1 local-stt-up
curl http://127.0.0.1:8003/health
```

Then in **Voice AI → AI providers**:

1. **Add connection → Deepgram**, name it "Live speech", base URL `http://local-stt:8000`.
2. Under **Speed**, apply the **Private streaming (self-hosted)** preset. It needs this
   connection, the self-hosted language model connection and the Pocket TTS connection.
   To choose by hand instead: Speech-to-text *Live speech*, model `zipformer-en-streaming`,
   language `en`.

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

### Licensing, checked at the source

- Model: `sherpa-onnx-streaming-zipformer-en-2023-06-26`, **Apache 2.0** on its model card
  ([csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26)).
  It is published by the sherpa-onnx maintainers in the official model list
  ([streaming Zipformer models](https://k2-fsa.github.io/sherpa/onnx/pretrained_models/online-transducer/zipformer-transducer-models.html))
  and as a release archive
  ([sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2](https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-streaming-zipformer-en-2023-06-26.tar.bz2)).
  It was trained with icefall on LibriSpeech, which is CC BY 4.0
  ([openslr.org/12](https://www.openslr.org/12)). The upstream training repository
  ([Zengwei/icefall-asr-librispeech-streaming-zipformer-2023-05-17](https://huggingface.co/Zengwei/icefall-asr-librispeech-streaming-zipformer-2023-05-17))
  states no license of its own.
- The server downloads only the four files it uses, about 75 MB, from the model repository
  at a fixed commit (`672fbf1b`) and checks each file's SHA-256 (`fetch_model.py`).
- Software: sherpa-onnx (Apache 2.0, [k2-fsa/sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx)),
  which bundles ONNX Runtime (MIT); websockets (BSD 3-Clause); NumPy (BSD 3-Clause, with
  bundled parts under 0BSD, MIT, Zlib and CC0).
- Not used, because of their licenses:
  - The LibriSpeech + GigaSpeech model `sherpa-onnx-streaming-zipformer-en-2023-06-21`.
    GigaSpeech's terms allow only non-commercial research use of the audio, and say a
    trained model only "may be eligible" for commercial use
    ([speechcolab/gigaspeech](https://huggingface.co/datasets/speechcolab/gigaspeech)).
  - The Kroko community models. They are CC BY-SA, presented for hobby, research and free
    tiers, with a separate commercial licence ([Banafo/Kroko-ASR](https://huggingface.co/Banafo/Kroko-ASR)).

### Languages

English only. For Urdu or mixed-language calls keep Whisper (`Systran/faster-whisper-small`
or larger) on the speech server.

### What the server supports

| Part of the protocol | Support |
|---|---|
| `wss/ws …/v1/listen`, 16-bit PCM (`encoding=linear16`), mono, any `sample_rate` from 8,000 to 48,000 Hz | Yes; audio is resampled to the model's 16 kHz |
| Interim results (`interim_results=true`) | Yes, whenever the recognised words change (about every 0.32 s of audio) |
| Final results at a pause (`is_final`, `speech_final`) | Yes, after `STT_ENDPOINT_SILENCE_MS` of silence; `endpointing=false` turns this off |
| `Finalize` → final result with `from_finalize: true` | Yes, immediately (an empty final if nothing was pending) |
| `KeepAlive`, `CloseStream` (→ last final, then `Metadata`) | Yes |
| `utterance_end_ms` (`UtteranceEnd`), `vad_events` (`SpeechStarted`) | Yes, approximated from recognised words |
| `keyterm`, `punctuate`, `smart_format`, `diarize`, `language`, other options | Accepted and ignored |
| `Authorization` header | Accepted as is, unless `STT_API_KEY` is set |
| Other encodings (μ-law, Opus), multichannel | Refused with HTTP 400 (the voice service sends linear16) |
| Flux (`/v2/listen`) | No |

### Streaming server settings

| Variable | Default | Meaning |
|---|---|---|
| `STT_NUM_THREADS` | `2` | CPU threads per decode. Recognition runs while the caller talks, the language model after. |
| `STT_MAX_SESSIONS` | `4` | Calls transcribed at once; more are refused (HTTP 503) and the voice service retries |
| `STT_ENDPOINT_SILENCE_MS` | `400` | Silence after words that ends a segment when no Finalize arrives first |
| `STT_FINALIZE_PAD_MS` | `800` | Silence added on Finalize so the last word is fully processed. The model emits a word's last sound up to about half a second late; with 400 ms, the last letter of a sentence was measured spilling into the caller's next turn. |
| `STT_MAX_UTTERANCE_S` | `20` | A segment longer than this is finalised anyway |
| `STT_IDLE_TIMEOUT_S` | `30` | A connection that sends nothing (not even KeepAlive) for this long is closed |
| `STT_STREAM_RECYCLE_S` | `300` | After this much audio a fresh recognizer stream is started between utterances, so memory stays flat on long calls |
| `STT_MODEL_ID` | `zipformer-en-streaming` | Name reported in results and `/health` |
| `STT_API_KEY` | unset | If set, clients must send `Authorization: Token <key>` (or `Bearer`) |

Transcripts are never written to the server log at the default level.

To measure on your machine (the voice service must be running):

```bash
make local-stt-up
# speech.wav: any 16-bit mono WAV; put its exact words in --text
docker cp speech.wav atlas-voice-runtime-1:/tmp/stt-bench/speech.wav
docker compose --profile voice exec -T voice-runtime /opt/venv/bin/python - \
  --wav /tmp/stt-bench/speech.wav --text "the words spoken" \
  < infra/scripts/bench_streaming_stt.py
```

## Pocket TTS or Kokoro

Both run on CPU, stream audio, and answer the same OpenAI-style speech request.

| | Kokoro 82M (in `local-speech`) | Pocket TTS 100M (`local-tts`) |
|---|---|---|
| Publisher | hexgrad, served by the speech server | Kyutai, served by ATLAS's small adapter |
| Weights license | Apache 2.0 (upstream `hexgrad/Kokoro-82M`; the ONNX repack card states none) | CC BY 4.0: commercial use allowed, with attribution to Kyutai |
| Code license | Apache 2.0 (speech server: MIT) | MIT |
| Languages | English and several others, by voice | English, French, German, Portuguese, Italian, Spanish, Dutch (one language per server) |
| Voices | Built-in voice packs (`af_heart` …) | 27 built-in voices; ATLAS allows only those licensed for commercial use (below) |
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
  log in and accept terms) and holds the voice-cloning weights. ATLAS does not use it.
  The library falls back to
  [kyutai/pocket-tts-without-voice-cloning](https://huggingface.co/kyutai/pocket-tts-without-voice-cloning),
  which is **not gated**, is also CC BY 4.0, and holds the built-in voices. An anonymous
  download returns HTTP 200 there and 401 from the gated repository.
- Acceptable use (from the model card): no voice impersonation or cloning without
  consent, no deception (including fraudulent calls or presenting generated speech as a
  real person), no unlawful or harmful content. The ATLAS adapter accepts only built-in
  voice names: no uploads or URLs, so it cannot clone a voice.
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

The adapter supports `response_format` `pcm` (24 kHz, 16-bit, mono, as the voice service
expects) and `wav`, and streams audio as it is generated. It runs one synthesis at a time,
because the model is not thread-safe. Pocket TTS has no speed control, so `speed` is
accepted and ignored.

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

Also measured on 2026-10-19 and left at their defaults (fresh 740-token prompt, configs
measured in alternation; `docs/perf/local-llm-tuning.json`):

| Option | Tried | Result |
|---|---|---|
| `num_thread` | 4, 5, 6 | 6 (all cores, the default) is fastest: 4 is 19% and 5 is 11% slower to first token |
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
| `WHISPER__CPU_THREADS` | `4` | Whisper threads. Speech-to-text and the language model run one after the other, so they don't compete. |
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
| This kind of CPU (4–8 cores, no GPU) | Fine for building and testing agents. Measured here: replies start about 3.2 s after the caller stops with Whisper and Pocket TTS, about 1.3 s with the streaming server (English), and for the first reply of a call about the same when ATLAS's prompt warm-up finished in
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
| Fastest speech recognition | English: the streaming server (`local-stt`). Other languages: `Systran/faster-whisper-base` (default) or `tiny` (much less accurate) |
| Fastest first audio on CPU | Try Pocket TTS and compare with the benchmark |

Override the defaults when downloading:

```bash
LLM_MODEL=llama3.2:1b STT_MODEL=Systran/faster-whisper-small make local-ai-models
```

## Privacy note

With self-hosted models selected, audio and text stay on your infrastructure. Pocket TTS
and the streaming server contact Hugging Face only to download their models on first
start. Saving the Deepgram connection for the streaming server sends its API key (no audio)
to Deepgram once, as described above. Two
optional features of the underlying voice service contact an external service even then:
knowledge-base document parsing, and the hosted trial models (disabled by default; see
`ATLAS_VOICE_ALLOW_HOSTED_TRIAL_MODELS`). Review these before deploying in a regulated
environment.
