# Running AI models on your own hardware

ATLAS agents need three models on every call: **speech-to-text** (hear the caller), a
**language model** (decide what to say), and **text-to-speech** (speak). Each can be a
cloud provider with your own API key, or a model running on your own servers. Self-hosted
models keep call audio and transcripts inside your network.

## What ships with ATLAS

The `local-ai` Docker profile runs two OpenAI-compatible servers on CPU:

| Service | Image | Serves | Default model |
|---|---|---|---|
| `local-llm` | `ollama/ollama:0.40.6` | Language model | `qwen2.5:1.5b` (≈1 GB) |
| `local-speech` | `ghcr.io/speaches-ai/speaches:0.8.3-cpu` | Speech-to-text and voice | Whisper small (multilingual) + Kokoro 82M |

Model files are stored in Docker volumes (`local_llm_models`, `local_speech_models`),
which live on the Docker data disk, so nothing is written to the system drive.

## Set up (one time)

```bash
make local-ai-up        # Windows: .\make.ps1 local-ai-up
make local-ai-models    # downloads about 2 GB of models
```

Then in **Voice AI → AI providers**:

1. **Add connection → Self-hosted models**, name it "Local LLM", base URL
   `http://local-llm:11434/v1`.
2. **Add connection → Self-hosted models**, name it "Local speech", base URL
   `http://local-speech:8000/v1`.
3. Under **Models for calls** choose:
   - Speech-to-text: *Local speech*, model `Systran/faster-whisper-small`
   - Language model: *Local LLM*, model `qwen2.5:1.5b`
   - Voice: *Local speech*, model `kokoro`, voice `af_heart`
4. **Save models**, then **Test providers**.

The hostnames `local-llm` and `local-speech` resolve inside the Docker network that the
voice service runs on, so the model servers never need to be exposed publicly.

## What to expect on CPU

Measured on the development machine (Intel i5-8500T, 6 cores, no GPU), each agent reply
goes through STT → LLM → TTS in sequence, so latency adds up. Expect replies to take
**several seconds**. That's fine for building and testing agents, but too slow for
production calls.

| Use | Recommendation |
|---|---|
| Building and testing agents | CPU with models ≤ 3B parameters, as above |
| Production calls | A GPU server (e.g. one 24 GB GPU runs an 8B model, Whisper and TTS comfortably), or cloud providers |

To use a GPU server instead, run the same images with GPU support on that machine and
point the connections' base URLs at it. Nothing else changes.

## Choosing models

| Need | Option |
|---|---|
| Better reasoning | `llama3.2:3b`, `qwen2.5:3b`, or 7–8B models on a GPU |
| Urdu or other languages | Keep the multilingual Whisper (`faster-whisper-small` or larger); choose an LLM trained on the language |
| Faster speech recognition | `Systran/faster-whisper-base` (less accurate) |

Override the defaults when downloading:

```bash
LLM_MODEL=llama3.2:3b make local-ai-models
```

## Privacy note

With self-hosted models selected, audio and text stay on your infrastructure. Two
optional features of the underlying voice service contact an external service even then:
knowledge-base document parsing, and the hosted trial models (disabled by default; see
`ATLAS_VOICE_ALLOW_HOSTED_TRIAL_MODELS`). Review these before deploying in a regulated
environment.
