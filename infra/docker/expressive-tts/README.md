# Expressive voice: Chatterbox-Turbo with human sounds

An OpenAI-compatible speech server around [Resemble AI Chatterbox-Turbo](https://huggingface.co/ResembleAI/chatterbox-turbo)
(350M parameters, English, MIT). It is a drop-in for the Pocket TTS adapter
([../pocket-tts](../pocket-tts)): the same request, the same 24 kHz 16-bit mono audio, the same phrase cache, and the
same voice and cloning endpoints. What it adds is sounds: a reply can carry `[laugh]`, `[sigh]`, `[gasp]` and six
more tags, and the voice performs them instead of reading them out.

It needs an NVIDIA GPU for live calls (a T4 carries about 2 simultaneous calls, see
[Measured](#measured-on-a-t4)). It runs on a CPU too, several times slower than real time: fine for trying it, not
for calls.

## Running it

```bash
docker build -t atlas/expressive-tts:chatterbox-turbo-0.1.7 infra/docker/expressive-tts
docker run -d --name expressive-tts --gpus all -p 127.0.0.1:8004:8000 \
  -v expressive_tts_models:/home/app/.cache atlas/expressive-tts:chatterbox-turbo-0.1.7
```

The first start downloads about 3 GB of weights into the volume (no Hugging Face token: the weights are ungated, and
the server never sends one), then loads the model, warms it up and makes the reply openers; `GET /health` answers
`{"status": "ok"}` after that. The host needs the NVIDIA driver (CUDA 12.4 or newer) and the NVIDIA Container
Toolkit; the CUDA libraries come with the pinned torch wheels.

In ATLAS, add a "Self-hosted models" connection (the speaches provider) with base URL
`http://expressive-tts:8000/v1`, model `chatterbox-turbo` and voice `default` (or a cloned voice's id), plus the API
key if `EXPRESSIVE_TTS_API_KEY` is set.

## Endpoints

All but `/health` need `Authorization: Bearer <EXPRESSIVE_TTS_API_KEY>` when that is set.

| Endpoint | Answer |
|---|---|
| `GET /health` | `{"status": "ok", "device": "cuda"}` once the model is loaded and warm (`"loading"` before) |
| `GET /v1/models` | the model id `chatterbox-turbo`, its sample rate, voices and sound tags |
| `GET /v1/audio/voices` | the built-in voice: `[{"id": "default", "license": "MIT", "default": true}]` |
| `GET /v1/audio/sound-tags` | `{"tags": ["[laugh]", "[chuckle]", ...]}`: the sounds this voice performs |
| `POST /v1/audio/speech` | `{model, input, voice, response_format: "pcm" or "wav", speed?}`: raw 24 kHz 16-bit mono PCM (`audio/pcm`) or the same with a streaming WAV header (`audio/wav`), sent sentence by sentence; header `X-Phrase-Cache: hit`, `opener` or `miss` |
| `GET /v1/audio/phrase-cache` | phrase cache counts (never the texts) |
| `GET /v1/voices` | built-in and cloned voices |
| `POST /v1/voices` | clone a voice (multipart: `name`, `audio` WAV, `consent=true`, optional `consent_statement`); 201 with the voice |
| `DELETE /v1/voices/{id}` | delete a cloned voice (204) |
| `GET /v1/cloning` | `{"state": "on", "message": null, "terms_url": "https://huggingface.co/ResembleAI/chatterbox-turbo", "token_source": null}` |
| `PUT /v1/cloning` | 200 with state `on` and a message: cloning needs no token here; the request body is never read, so a token sent to it is neither used nor kept |
| `DELETE /v1/cloning` | 409: cloning needs no token, so there is nothing to remove (the operator can turn it off, below) |

`speed` is accepted and ignored (the model has no speed control), as are `instructions`; OpenAI voice names (`alloy`,
`nova`, ...) mean the default voice. Requests for other formats or sample rates are refused (422), as with the
Pocket adapter.

## Sound tags

The tags are the nine of Resemble AI's own Turbo demo (`EVENT_TAGS` in `gradio_tts_turbo_app.py`), each a single token
of the model's tokenizer:

`[laugh]` `[chuckle]` `[sigh]` `[gasp]` `[cough]` `[clear throat]` `[groan]` `[sniff]` `[shush]`

- The forms a language model writes are mapped to them, the same forms the Pocket adapter removes: `[Laughs]`,
  `[laughed]`, `[laughter]`, `<laugh>`, `[clears his throat]`, `[sniffling]`, `[shhh]`, ...
- Any other bracketed text is removed before synthesis, so it is never read aloud: `[pause]`, `[smiles warmly]`,
  `<break time="1s"/>`, `</laugh>`, stray `[` or `]`; so are emoji. Text that is only such brackets or emoji gives empty
  audio (200).
- The tokenizer also holds undocumented style tokens (`[happy]`, `[angry]`, `[whispering]`, ...). They are not used:
  in the evaluation `[surprised]` turned "Wow!" into "Well, how?". `EXPRESSIVE_TTS_SOUND_TAGS` can choose others; at
  startup any tag that is not a single token of the tokenizer is dropped with a warning (it would be read out).
- A tag stays with its sentence: one right after a sentence end opens the next sentence (`Realty. [laugh] Sorry,
  ...` is made as `[laugh] Sorry, ...`), one at the very end closes the last sentence, a tag alone is spoken alone.
- ATLAS can read `GET /v1/audio/sound-tags` to know this voice performs sounds, and tell the agent which tags it may
  write.

## Sentences, first audio and the phrase cache

Turbo makes a whole sentence at a time (it does not stream within one). The server splits a reply into sentences
(not after "Dr.", "p.m.", initials or a lowercase continuation) and sends each one's audio as soon as it is made. A
first sentence longer than 60 characters is cut after its first clause mark (`,` `;` `:` `...` ` - `) past 20
characters, so the reply starts sooner; any piece longer than 300 characters is cut at clause marks, else between
words (Turbo makes at most 40 s per call).

The phrase cache is the Pocket adapter's, with the same settings (`EXPRESSIVE_TTS_` instead of `POCKET_TTS_`): the
same 26 reply openers ("Got it.", "Okay.", "Sure.", "Oh nice!", "Ha, fair enough.", ...) are made at startup for the
default voice and kept in memory, so a reply that opens with one starts at once ("Got it, perfect timing." plays the
kept "Got it." while "Perfect timing." is made). Any other voice, a cloned one too, gets them made in the background
the first time it speaks (after live speech that would run out of audio sooner). Other sentences of up to 32
characters are kept once spoken, least recently used evicted first, 16 MB at most.

One sentence is made at a time on the model. Waiting sentences go earliest deadline first: a new reply's first
sentence goes before the later sentences of replies already playing, and those go in the order their callers would
run out of audio.

## Voices and cloning

- **Built-in voice `default`**: `conds.pt` of the model repository (MIT). Resemble AI does not say whose recording it
  was made from. The package ships no other reference voices.
- **Cloned voices**: Chatterbox clones from a reference clip, with no gated weights and no token, so cloning is always
  on. The contract is the Pocket adapter's: a WAV of 5-30 s (8-96 kHz, any channel count, at most 10 MB), `consent`
  must be `true` (the statement is kept with the voice), names up to 60 characters, at most 50 voices
  (`EXPRESSIVE_TTS_MAX_CUSTOM_VOICES`), ids `custom-<name>-<16 hex>`. The model uses the first 10-15 s: about 10 s of
  clear speech is best. Turbo is English only; a cloned voice keeps the accent of its sample.
- The voice's conditioning (speaker embedding, speech-token prompt, decoder reference) is computed once when the voice
  is made and kept as safetensors (about 0.2 MB) with a JSON record in `voices/` of the volume; the uploaded audio is
  decoded in memory and never written. Up to 16 cloned voices are held on the GPU (least recently used evicted), so a
  cloned voice costs nothing extra per request.
- `EXPRESSIVE_TTS_MAX_CUSTOM_VOICES=0` turns cloning off: `/v1/voices` answers 501, `GET /v1/cloning` says `off`.
- The model card asks not to use the model "to do bad things": clone a voice only with the speaker's consent, never to
  deceive anyone or to pass generated speech off as a real person.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `EXPRESSIVE_TTS_MODEL_ID` | `chatterbox-turbo` | id clients send as `model` |
| `EXPRESSIVE_TTS_MODEL_REVISION` | `749d1c1a46eb...` | revision of `ResembleAI/chatterbox-turbo` to download (the one measured) |
| `EXPRESSIVE_TTS_DEVICE` | `auto` | `auto` (the GPU when there is one), `cuda` or `cpu` |
| `EXPRESSIVE_TTS_DEFAULT_VOICE` | `default` | voice for OpenAI voice names |
| `EXPRESSIVE_TTS_SOUND_TAGS` | the nine above | comma-separated sounds to perform |
| `EXPRESSIVE_TTS_API_KEY` | none | require `Authorization: Bearer <key>` |
| `EXPRESSIVE_TTS_WARMUP` | `1` | one synthesis at startup (the first takes about 17 s: CUDA kernels and the watermarker) |
| `EXPRESSIVE_TTS_FIRST_SENTENCE_CHARS` | `60` | cut a longer first sentence after its first clause mark (`0` = never) |
| `EXPRESSIVE_TTS_MAX_SENTENCE_CHARS` | `300` | cut longer sentences |
| `EXPRESSIVE_TTS_VOICE_DIR` | `/home/app/.cache/voices` | cloned voices (inside the volume) |
| `EXPRESSIVE_TTS_MAX_CUSTOM_VOICES` | `50` | most cloned voices on disk; `0` turns cloning off |
| `EXPRESSIVE_TTS_CUSTOM_VOICE_CACHE` | `16` | cloned voices held in memory |
| `EXPRESSIVE_TTS_PHRASE_CACHE` | the 26 openers | `\|`-separated phrases made at startup (`""` = none) |
| `EXPRESSIVE_TTS_PHRASE_CACHE_VOICES` | the default voice | voices those phrases are made for at startup |
| `EXPRESSIVE_TTS_PHRASE_CACHE_ANY_VOICE` | `true` | other voices get them in the background the first time they speak |
| `EXPRESSIVE_TTS_PHRASE_CACHE_MAX_CHARS` | `32` | other sentences up to this length are kept once spoken (`0` = only the openers) |
| `EXPRESSIVE_TTS_PHRASE_CACHE_MB` | `16` | memory for kept audio |
| `LOG_LEVEL` | `INFO` | |

Logs carry lengths and counts only (characters, sentences, tags kept and removed, seconds of audio, first-audio
time), never the text or the audio, nor a token.

## Measured on a T4

Measured 2026-10-22 on a Kaggle T4 (16 GB): this server on 127.0.0.1 inside the session, installed from this
folder's `requirements.txt` as is (Python 3.13.15, torch 2.6.0+cu124), measured through HTTP. First audio is the time
from sending the request to the first byte of audio; a sentence arrives whole, so it is also the time to the first
0.5 s of audio. The 8 call lines are those of the earlier evaluation (four with a tag). Raw results:
`.cache/voice-lab/expressive-tts/` (not in the repository).

| Requests | First audio p50 / p90 |
|---|---:|
| Phrase cache off, 8 lines x 3 | 0.87 s / 1.44 s |
| Defaults: openers kept at startup, same lines | 0.57 s / 1.36 s (lines opening with "Got it." or "Perfect!", and short sentences already spoken, start at once) |
| Each line opened with a kept opener ("Okay. ...") | 0.003 s, then a 0.3-0.8 s pause in 5 of 8 lines while the next sentence is made |

The pause after a kept opener is the cost of a model that does not stream: "Okay." plays for about half a second,
making the next sentence takes 0.5-1.5 s.

**Simultaneous calls** (phrase cache off unless stated). "Back-to-back": each caller asks for its next line as soon as
the last one has arrived. "Conversation": it waits until its audio has played, plus 1.5 s for the caller to answer. A
pause is a moment where a player that buffers 0.5 s runs out of audio.

| Callers | Mode | Speech made per second | First audio p50 / p90 | Requests with a pause > 0.2 s |
|---:|---|---:|---:|---:|
| 1 | back-to-back | 2.02 s | 0.84 s / 1.47 s | 1 of 8 |
| 2 | back-to-back | 2.03 s | 1.82 s / 2.55 s | 8 of 16 |
| 3 | back-to-back | 2.02 s | 2.18 s / 3.73 s | 21 of 24 |
| 4 | back-to-back | 2.04 s | 3.52 s / 5.10 s | 28 of 32 |
| 2 | conversation | 1.23 s | 1.06 s / 2.11 s | 6 of 16 (longest 2.0 s) |
| 4 | conversation | 1.91 s | 1.78 s / 3.04 s | 26 of 32 |
| 6 | conversation | 1.98 s | 4.05 s / 5.84 s | 42 of 48 |
| 4 | conversation, openers kept | 2.72 s | 0.003 s / 0.003 s | 22 of 32 |

One T4 makes about 2 seconds of speech per second for all calls together (one sentence at a time): it carries about
**2 simultaneous calls**; from 3 on, replies start late and pause.

- **Sound tags**: each of the nine tags in a sentence, against the same sentence without it: no tag was read aloud
  (Whisper small.en transcripts), and the tagged sentence was 0.04-0.72 s longer. Whisper wrote some of the sounds
  down: "Heh heh heh." for `[laugh]` alone, "Ugh," for `[groan]`, "KA-KUN!" for `[clear throat]`. Text with unknown
  tags and aliases (`[pause]`, `[smiles warmly]`, `<break .../>`, `[Laughs]`, `[whispering]`) was transcribed as
  exactly its words. Of the 8 lines, 7 were transcribed without error; line 1 had three words misheard ("Demo
  Realty", "mood", "Is").
- **Cloning** from a 14.4 s public-domain LJ Speech sample: 2.9 s. Speaker similarity to the sample (WavLM-base-plus-sv
  cosine): 0.96 for the cloned voice, 0.69 for the built-in voice, 0.96 again after a restart (the voice loaded from
  disk). First audio is the same as with the built-in voice.
- **Start**: 60-73 s the first time (3 GB download, load, a 15-17 s cold warm-up, the openers); 29 s afterwards with
  the 15 openers of the first run (8.5 s of it); the 26 openers of the final version take 17 s.
- **A new voice's openers** (a cloned voice's first reply starts them in the background): 26 in 33 s. Meanwhile live
  replies that miss the cache started up to about 1 s later (2.0-2.2 s instead of 1.1 s): a phrase being made is
  not interrupted. Afterwards that voice's replies open from memory too (first audio 3 ms). The final version also
  lets the reply that started them go first (it waited 1 s behind the first phrase in the measurement);
  `EXPRESSIVE_TTS_PHRASE_CACHE_ANY_VOICE=false` turns this off.
- **Memory**: 3.0 GB of GPU memory once loaded, 3.2 GB at most with 4 calls. Host RAM 2.4-3.4 GB, 4.9-5.4 GB at most
  while loading.
- **CPU fallback** (a Kaggle CPU session, 4 vCPU, same install): it works, about 5 times slower than real time.
  First audio 11.7-14.5 s (a kept opener still at once); ready after 187 s, 115 s of which made the 15 openers of
  that run (the 26 of the final version take about 200 s on such a CPU; `EXPRESSIVE_TTS_PHRASE_CACHE=""` skips
  them); cloning 4.6 s; host RAM 4.4 GB, 6.5 GB at most.

## Licences and watermark

- Model weights and `conds.pt` (the built-in voice): MIT, `ResembleAI/chatterbox-turbo`, ungated. Code: `chatterbox-tts`
  0.1.7, MIT. Watermarker: `resemble-perth` 1.0.1, MIT.
- Every output carries Resemble AI's Perth watermark: imperceptible, it survives MP3 compression and editing and can be
  detected with `perth.PerthImplicitWatermarker().get_watermark(...)`. The library adds it; the server refuses to
  start if the watermarker cannot load (it needs `setuptools<81`, pinned).
- The image installs only what Turbo uses: `chatterbox-tts` also lists gradio, spacy-pkuseg and pykakasi (its demo app
  and the multilingual model), which are left out; every package is pinned in `requirements.txt` and installed with
  `--no-deps`.

## Tests

`test_app.py` replaces the model with a fake (no GPU, no download, no torch) and covers sound tags, sentence
splitting, streaming order, the scheduling of waiting sentences, the phrase cache, voices and cloning, the API key
and that logs never hold the text:

```bash
python -m unittest discover -s infra/docker/expressive-tts -t infra/docker/expressive-tts -v
```

It needs fastapi, numpy, scipy, safetensors, soxr, httpx, python-multipart and uvicorn (a small venv), or run it in
the image with the folder mounted (see the file's docstring).
