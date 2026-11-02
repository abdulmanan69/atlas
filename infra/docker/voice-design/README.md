# Voice design: Qwen3-TTS, a voice from a description

An OpenAI-compatible speech server around [Qwen3-TTS](https://github.com/QwenLM/Qwen3-TTS) (Alibaba Qwen, Apache-2.0).
It is a drop-in for the Pocket TTS adapter ([../pocket-tts](../pocket-tts)) and the expressive server
([../expressive-tts](../expressive-tts)): the same request, the same 24 kHz 16-bit mono audio, the same phrase cache,
the same voice and cloning endpoints. What it adds is a voice from words: `POST /v1/voices/design` takes a plain
description ("warm, upbeat American man in his forties, clear and friendly") and makes a new voice from it; no
recording, no speaker, no consent. It also clones a voice from a recording of a consenting speaker, from 3 seconds.

Two models work together, both from the Qwen3-TTS release (Hugging Face, ungated, Apache-2.0):

| Role | Model | Size on disk | What it does |
|---|---|---:|---|
| design | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | 4.5 GB | renders a short identity sample of a described voice, once, when the voice is designed (and the built-in `default` voice at the first start) |
| speech | `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | 2.5 GB | speaks every reply, cloning from a sample: a designed voice's identity sample (with its known transcript) is the reference, so the voice stays the same from reply to reply; the same model clones uploaded recordings |

The VoiceDesign model exists only at 1.7B; the Base model at 0.6B (default, the faster one) and 1.7B
(`VOICE_DESIGN_SPEECH_MODEL=Qwen/Qwen3-TTS-12Hz-1.7B-Base`, measured as slower, not measured here). Why not speak with
the design model directly? Its output is sampled: the same description gives a different speaker every time
([measured](#measured-on-a-t4)), and cloning the one rendered sample is what keeps a designed voice consistent.

It needs an NVIDIA GPU for live calls. It runs on a CPU too, many times slower than real time: fine for trying it, not
for calls.

## Running it

```bash
docker build -t atlas/voice-design:qwen3-tts-0.1.1 infra/docker/voice-design
docker run -d --name voice-design --gpus all -p 127.0.0.1:8005:8000 \
  -v voice_design_models:/home/app/.cache atlas/voice-design:qwen3-tts-0.1.1
```

The first start downloads about 7 GB of weights into the volume (no Hugging Face token: the weights are ungated, and
the server never sends one), loads both models, designs the built-in `default` voice from
`VOICE_DESIGN_DEFAULT_VOICE_DESCRIPTION` and keeps it in the volume, warms up and makes the reply openers;
`GET /health` answers `{"status": "ok"}` after that. The host needs the NVIDIA driver (CUDA 12.4 or newer) and the
NVIDIA Container Toolkit; the CUDA libraries come with the pinned torch wheels. The models run in bfloat16 on a GPU
that computes it natively (compute capability 8: A10G, L4, A100, ...) and in float32 on older ones such as the T4
(`VOICE_DESIGN_DTYPE`). Not float16: the speech model's code predictor overflows it (NaN logits at the first synthesis
on a T4, [measured](#measured-on-a-t4)). A T4 cannot run flash-attention 2 either, so attention is `sdpa`.

In ATLAS, add a "Self-hosted models" connection (the speaches provider) with base URL `http://voice-design:8000/v1`,
model `qwen3-tts` and voice `default` (or a designed or cloned voice's id), plus the API key if `VOICE_DESIGN_API_KEY`
is set. ATLAS reads `GET /v1/capabilities` to know this server designs voices, and shows the design and preview
controls on its voice-cloning screen; the cloning screen itself works unchanged (`/v1/voices` and `/v1/cloning` keep
their shapes).

## Endpoints

All but `/health` need `Authorization: Bearer <VOICE_DESIGN_API_KEY>` when that is set.

| Endpoint | Answer |
|---|---|
| `GET /health` | `{"status": "ok", "device": "cuda"}` once both models are loaded and warm (`"loading"` before) |
| `GET /v1/models` | the model id `qwen3-tts`, its `variant` (`0.6B-Base + 1.7B-VoiceDesign`), the two repositories, languages, sample rate, voices and (empty) sound tags |
| `GET /v1/capabilities` | `{"model": "qwen3-tts", "cloning": true, "design": true, "instruct": false, "sound_tags": [], "languages": ["English", "Chinese", "Japanese", "Korean", "German", "French", "Russian", "Portuguese", "Spanish", "Italian"], "streaming": "sentence", "reference_seconds": {"min": 3, "max": 30}}` (`instruct` is `true` with `VOICE_DESIGN_INSTRUCT=design`, `cloning` false with `VOICE_DESIGN_CLONING=false`, `design` false without the design model) |
| `GET /v1/audio/voices` | the built-in voice: `[{"id": "default", "license": "Apache-2.0", "default": true}]` |
| `GET /v1/audio/sound-tags` | `{"tags": []}`: this voice performs no sounds |
| `POST /v1/audio/speech` | `{model, input, voice, response_format: "pcm" or "wav", speed?, instructions?}`: raw 24 kHz 16-bit mono PCM (`audio/pcm`) or the same with a streaming WAV header (`audio/wav`), sent sentence by sentence; header `X-Phrase-Cache: hit`, `opener`, `miss` or `bypass` |
| `GET /v1/audio/phrase-cache` | phrase cache counts (never the texts) |
| `GET /v1/voices` | built-in, designed and cloned voices; a designed voice is `{"id": "design-<name>-<16 hex>", "name", "type": "designed", "description", "language", "seed", "created_at", "sample_seconds"}` |
| `POST /v1/voices/design` | JSON `{name, description, language?, sample_text?, seed?}`; 201 with the voice object above |
| `GET /v1/voices/{id}/sample` | a short WAV of the voice, for a preview: a designed voice's identity sample, a cloned voice's rendered preview (never the uploaded recording); 404 without one |
| `POST /v1/voices` | clone a voice (multipart: `name`, `audio` WAV of 3-30 s, `consent=true`, optional `consent_statement`, optional `transcript`); 201 with the voice |
| `DELETE /v1/voices/{id}` | delete a designed or cloned voice (204) |
| `GET /v1/cloning` | `{"state": "on", "message": null, "terms_url": "https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base", "token_source": null}` (`off` with its reason when the operator turned cloning off) |
| `PUT /v1/cloning` | 200 with state `on` and a message: cloning needs no token here; the request body is never read, so a token sent to it is neither used nor kept |
| `DELETE /v1/cloning` | 409: cloning needs no token, so there is nothing to remove (the operator can turn it off, below) |

`speed` is accepted and ignored (the model has no speed control); OpenAI voice names (`alloy`, `nova`, ...) mean the
default voice. Requests for other formats or sample rates are refused (422), as with the other servers.

## Designing a voice

- `description`: who is speaking and how, in plain words, 3-1000 characters (ATLAS allows 10-300): age, gender,
  accent, warmth, pace, mood. The design model takes it as its natural-language instruction. Not a real person's name:
  the model would not know them, and the voice is meant to be nobody's.
- `language`: one of the ten (any case) or `Auto`; the server's `VOICE_DESIGN_LANGUAGE` (English) when empty. It is the
  language of the identity sample and of everything the voice says later.
- `sample_text`: what the identity sample says (up to 300 characters); the server's own line when empty
  (`VOICE_DESIGN_SAMPLE_TEXT`, 100 characters, about 7 s). The speech model continues this sample for every reply
  (in-context cloning), so a sample that sounds the way the voice should speak gives the best results.
- `seed`: makes the design repeatable on the same hardware and software; without one the model samples freely and two
  designs from one description differ.
- What is kept, in `voices/` of the volume: the speech model's clone prompt (speaker embedding and the sample's codec
  tokens, safetensors, about 0.1 MB), the identity sample as WAV (about 0.3 MB) and a JSON record (name, description,
  language, seed, sample text, dates). A voice costs nothing extra per request: up to 16 prompts are held in memory
  (`VOICE_DESIGN_VOICE_CACHE`), least recently used evicted.
- Designed voices work in `POST /v1/audio/speech` by id like any other, with the phrase cache: the 26 openers ("Got
  it.", "Okay.", "Sure.", ...) are made for the `default` voice at startup and for any other voice in the background the
  first time it speaks (`VOICE_DESIGN_PHRASE_CACHE_ANY_VOICE`), so a reply that opens with one starts at once.
- The built-in `default` voice is a designed voice too (description, seed, language and sample text from the
  environment, made at the first start and kept); change any of them and it is made again at the next start. Without
  the design model (`VOICE_DESIGN_DESIGN_MODEL=""`) the server only starts if that voice is already in the volume.

## Instructions

`POST /v1/audio/speech` accepts `instructions` (a tone direction such as "cheerful, a little faster"). The speech
model that speaks designed and cloned voices (the Base model) takes none, so by default they are accepted and ignored
(logged as a count), and `capabilities.instruct` is `false`. With `VOICE_DESIGN_INSTRUCT=design` a designed voice's
reply with instructions is rendered by the design model from the voice's description plus the instructions instead
(cloned voices still ignore them): the delivery follows the instructions, and the speaker drifts from the identity
sample by about as much as any two designs of one description do ([measured](#measured-on-a-t4)). Such replies bypass
the phrase cache (`X-Phrase-Cache: bypass`) and are not kept.

## Sound tags

None: Qwen3-TTS has no sound tokens (not documented, not in its tokenizer's added tokens). Every bracketed cue
(`[laugh]`, `[sigh]`, `<laugh>`), stage direction (`[pause]`, `[smiles warmly]`), markup (`<break time="1s"/>`),
stray square bracket and emoji is removed before synthesis, so it is never read aloud; text that is only such
brackets gives empty audio (200). `GET /v1/audio/sound-tags` answers `[]`, so ATLAS tells the agent to write none.

## Sentences, first audio and the phrase cache

The speech model makes a whole sentence at a time (qwen-tts 0.1.1 has no streaming generation: its
`non_streaming_mode` only changes how the text is fed in). The server splits a reply into sentences (not after "Dr.",
"p.m.", initials or a lowercase continuation) and sends each one's audio as soon as it is made. A first sentence longer
than 60 characters is cut after its first clause mark past 20 characters, so the reply starts sooner; any piece longer
than 300 characters is cut at clause marks, else between words. Each synthesis is bounded to about three times the
expected length (`token_cap`), so a generation that does not stop cannot run for minutes.

The phrase cache and the scheduling of waiting sentences are the expressive server's, with the same settings
(`VOICE_DESIGN_` instead of `EXPRESSIVE_TTS_`): see [../expressive-tts/README.md](../expressive-tts/README.md).

## Voice cloning

- `POST /v1/voices` takes a WAV of 3-30 s (8-96 kHz, any channel count, at most 10 MB); `consent` must be `true`
  (the statement is kept with the voice), names up to 60 characters, at most 50 voices in all
  (`VOICE_DESIGN_MAX_VOICES`), ids `custom-<name>-<16 hex>`. With a `transcript` of the sample the model continues the
  sample (in-context cloning, the better mode); without one only the speaker embedding is used (the mode ATLAS's
  cloning screen gets, since it sends no transcript).
- The uploaded audio is decoded in memory and never written: only the clone prompt (safetensors), a JSON record and a
  rendered preview (`VOICE_DESIGN_SAMPLE_TEXT` in the new voice) are kept.
- `VOICE_DESIGN_CLONING=false` turns cloning from recordings off: `POST /v1/voices` answers 501, `GET /v1/cloning`
  says `off`; designed and built-in voices keep working, and `GET /v1/voices` still lists them.
- The models' Apache-2.0 licence places no terms on their output, so the usual rule is ATLAS's own: clone a voice only
  with the speaker's consent, never to deceive anyone or to pass generated speech off as a real person.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `VOICE_DESIGN_MODEL_ID` | `qwen3-tts` | id clients send as `model` |
| `VOICE_DESIGN_SPEECH_MODEL` | `Qwen/Qwen3-TTS-12Hz-0.6B-Base` | the model that speaks and clones (`...-1.7B-Base` also works) |
| `VOICE_DESIGN_DESIGN_MODEL` | `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` | the design model (`""` = not loaded: no design) |
| `VOICE_DESIGN_SPEECH_REVISION`, `VOICE_DESIGN_DESIGN_REVISION` | the revisions measured (`5d83992436ea...`, `5ecdb67327fd...`) | revisions to download |
| `VOICE_DESIGN_DEVICE` | `auto` | `auto` (the GPU when there is one), `cuda` or `cpu` |
| `VOICE_DESIGN_DTYPE` | `auto` | `auto` (bfloat16 on a GPU of compute capability 8 or newer, float32 on older GPUs and on a CPU), `bfloat16`, `float32`; `float16` is accepted with a warning and overflows |
| `VOICE_DESIGN_ATTENTION` | `sdpa` | attention implementation passed to the models |
| `VOICE_DESIGN_LANGUAGE` | `English` | language of the built-in voice and of voices designed without one (`Auto` lets the model decide) |
| `VOICE_DESIGN_DEFAULT_VOICE_DESCRIPTION` | a warm, friendly American woman in her thirties, ... | the built-in voice |
| `VOICE_DESIGN_DEFAULT_VOICE_SEED` | `1234` | its seed |
| `VOICE_DESIGN_SAMPLE_TEXT` | "Hi, thanks for calling. I'm glad you reached out today; ..." | what identity samples and previews say |
| `VOICE_DESIGN_INSTRUCT` | `ignore` | `design`: honour `instructions` for designed voices through the design model |
| `VOICE_DESIGN_API_KEY` | none | require `Authorization: Bearer <key>` |
| `VOICE_DESIGN_WARMUP` | `1` | one synthesis at startup |
| `VOICE_DESIGN_FIRST_SENTENCE_CHARS` | `60` | cut a longer first sentence after its first clause mark (`0` = never) |
| `VOICE_DESIGN_MAX_SENTENCE_CHARS` | `300` | cut longer sentences |
| `VOICE_DESIGN_VOICE_DIR` | `/home/app/.cache/voices` | voices (inside the volume) |
| `VOICE_DESIGN_MAX_VOICES` | `50` | most designed and cloned voices on disk |
| `VOICE_DESIGN_CLONING` | `true` | `false` turns cloning from recordings off |
| `VOICE_DESIGN_VOICE_CACHE` | `16` | voices held in memory |
| `VOICE_DESIGN_PHRASE_CACHE`, `_VOICES`, `_ANY_VOICE`, `_MAX_CHARS`, `_MB` | as in the expressive server | the phrase cache |
| `LOG_LEVEL` | `INFO` | |

Logs carry lengths and counts only (characters, sentences, cues removed, seconds of audio, first-audio time, the
length of a description or of instructions), never a text, a description, a transcript or the audio, nor a token.

## Measured on a T4

Not measured yet. The first T4 run (float16) showed the install from `requirements.txt`
takes about 80 s, the speech model loads in 20 s and the design model in 34 s, and that
float16 is unusable for the Base model (a device-side assert in its code predictor), so the
server runs float32 on a T4 and bfloat16 on newer GPUs. The float32 run (startup, GPU
memory, first-audio latency per line, designed-voice consistency, instructions, cloning)
was started but its results were not collected before the Kaggle token expired; run the
harness in `.cache/voice-lab/voice-design/kaggle/` again (`build_kernel.py`, then push the
kernel) and summarise `results.json` with the summariser script it names to fill this in.

## Licences

- Code: `qwen-tts` 0.1.1 (PyPI), Apache-2.0, https://github.com/QwenLM/Qwen3-TTS (LICENSE: Apache License 2.0,
  copyright Alibaba Cloud).
- Weights, all ungated and Apache-2.0 on their model cards:
  `Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign` (https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign, revision
  `5ecdb67327fd37bb2e042aab12ff7391903235d3`), `Qwen/Qwen3-TTS-12Hz-0.6B-Base`
  (https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-Base, revision `5d83992436eae1d760afd27aff78a71d676296fc`),
  `Qwen/Qwen3-TTS-12Hz-1.7B-Base` (https://huggingface.co/Qwen/Qwen3-TTS-12Hz-1.7B-Base, revision
  `fd4b254389122332181a7c3db7f27e918eec64e3`, optional). Each repository carries its own copy of the speech tokenizer
  (`speech_tokenizer/`, the decoder that makes the 24 kHz audio); the standalone `Qwen/Qwen3-TTS-Tokenizer-12Hz` is
  Apache-2.0 as well and is not downloaded.
- The built-in `default` voice and every designed voice are the model's output: no recording of a person is involved.
  The `license` shown for `default` (`Apache-2.0`) is the licence of the model that made it; Apache-2.0 places no terms
  on outputs.
- No watermark: Qwen3-TTS adds none (the expressive server's Perth watermark is Resemble AI's, not available here).
- The image installs `qwen-tts` without its `gradio` extra (the demo app); `sox` and `onnxruntime` are installed because
  importing the package loads its 25 Hz tokenizer module, which imports them, although the 12 Hz models never run it.
  Every package is pinned in `requirements.txt` and installed with `--no-deps`.

## Tests

`test_app.py` replaces both models with a fake (no GPU, no download, no torch) and covers designing a voice, cloning,
the capabilities, `instructions`, text cleaning, streaming order, the phrase cache, the built-in voice made at the
first start, the API key and that logs never hold a text, a description or a transcript:

```bash
python -m unittest discover -s infra/docker/voice-design -t infra/docker/voice-design -v
```

It needs fastapi, numpy, scipy, safetensors, soxr, httpx, python-multipart and uvicorn (a small venv), or run it in
the image with the folder mounted (see the file's docstring).
