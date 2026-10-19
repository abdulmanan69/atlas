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

Model files are stored in Docker volumes (`local_llm_models`, `local_speech_models`,
`local_tts_models`), which live on the Docker data disk, so nothing is written to the
system drive.

## Set up (one time)

```bash
make local-ai-up        # Windows: .\make.ps1 local-ai-up
make local-ai-models    # downloads about 1.5 GB of models
make local-tts-up       # optional: builds and starts Pocket TTS (weights ≈0.3 GB on first start)
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

The hostnames `local-llm`, `local-speech` and `local-tts` resolve inside the Docker
network that the voice service runs on, so the model servers never need to be exposed
publicly. On the host they listen only on `127.0.0.1` (ports 11434, 8001 and 8002).

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
| Language model, first reply of a call | same, prompt not yet cached | **~6.1 s** to first token (5.8–6.4 s) | (same) |

What this means for one reply on this CPU, after the caller stops talking:

| Part | With Pocket TTS | With Kokoro |
|---|---|---|
| Speech-to-text, for a 5 s sentence | 2.0 s | 2.0 s |
| Language model to its first sentence (≈10 tokens), later turns | 1.0 s | 1.0 s |
| Voice to first audio | 0.2 s | 1.8 s |
| **Reply starts after** | **≈ 3.2 s** | **≈ 4.8 s** |

Three findings:

- **Pocket TTS is about 8.6× faster to first audio than Kokoro here** (212 vs 1,826 ms).
  Kokoro on this server sends a short sentence only once it is fully generated; Pocket TTS
  streams frame by frame. Use Pocket TTS for English agents.
- **The first reply of every call is slow (~6 s) on CPU.** The language model has to read
  the whole agent prompt once; afterwards it is cached and each turn only reads the new
  words (~0.3 s). The greeting is spoken before the first model reply, which hides part of
  this. Keep prompts short (the flow editor's token meter warns above 1,500 tokens), and
  see "Warming the prompt" below.
- **Speech-to-text is now the largest delay.** Self-hosted Whisper waits for the caller to
  finish and then transcribes everything (0.4× real time). Hosted streaming speech-to-text
  transcribes while the caller is still talking, which is why hosted stacks answer in
  under a second.

A faster CPU, a GPU or hosted speech-to-text changes these numbers a lot; re-run the
benchmark on the machine you deploy to.

### Warming the prompt (not built yet)

Ollama keeps the last prompt it read in memory. Sending the agent's system prompt to the
model while the phone is ringing would make the first reply as fast as later ones
(~0.3 s instead of ~6 s on this CPU). ATLAS does not do this yet; it is the next
self-hosted latency improvement worth building.

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
| `OLLAMA_FLASH_ATTENTION` | `1` | Supported on CPU (the default is "auto"). Saves memory as context grows; expect little speed change at these sizes. |
| `OLLAMA_NO_CLOUD` | `1` | No remote inference or web search, so everything stays local. |

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
| This kind of CPU (4–8 cores, no GPU) | Fine for building and testing agents. Measured here: replies start about 3.2 s after the caller stops (Pocket TTS), about 6 s for the first reply of a call. Too slow for production calls. |
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
| Fastest speech recognition | `Systran/faster-whisper-base` (default) or `tiny` (much less accurate) |
| Fastest first audio on CPU | Try Pocket TTS and compare with the benchmark |

Override the defaults when downloading:

```bash
LLM_MODEL=llama3.2:1b STT_MODEL=Systran/faster-whisper-small make local-ai-models
```

## Privacy note

With self-hosted models selected, audio and text stay on your infrastructure. Pocket TTS
contacts Hugging Face only to download its weights and voices on first start. Two
optional features of the underlying voice service contact an external service even then:
knowledge-base document parsing, and the hosted trial models (disabled by default; see
`ATLAS_VOICE_ALLOW_HOSTED_TRIAL_MODELS`). Review these before deploying in a regulated
environment.
