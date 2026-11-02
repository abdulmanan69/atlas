# Live speech-to-text on a GPU: Nemotron Speech Streaming against Parakeet TDT 0.6B v3

Measured 2026-10-30 on one NVIDIA Tesla T4 (16 GB, 4 vCPUs) in a private Kaggle notebook,
22 minutes of GPU time, benchmark only (public test sets, synthetic caller lines and synthetic
noise; no real-caller audio; every server on 127.0.0.1). Raw numbers:
[gpu-stt-streaming.json](gpu-stt-streaming.json). The question: for phone calls on a GPU, should
the live speech-to-text default stay Parakeet TDT 0.6B v3 (whole-utterance, `parakeet-0.6b-v3`)
or become NVIDIA Nemotron Speech Streaming EN 0.6B (cache-aware streaming, `nemotron-en-560`
with 560 ms chunks, or `nemotron-en-160`)?

**Recommendation: make `nemotron-en-560` the GPU default for calls, running its full-precision
export on CUDA.** Through the server's real protocol with Finalize 0.2 s after the caller stops,
its final transcript arrives 0.26 s after the caller's last word (p90 0.27 s) against 0.39 s
(p90 0.45 s) for Parakeet, and it holds that with four calls transcribed at once (p90 0.30 s,
where Parakeet's finals queue up to 1.05 s). Its live words already carried the caller's last
word about 80 ms after the caller stopped on 7 of the 8 caller lines, before the voice service
even sends Finalize. On 20 clips of breath, hum and line noise with no speech in them it wrote
nothing (0 of 40 at 16 and 8 kHz), where Parakeet wrote filler words ("Hmm.", "Ah", "Yeah.") on
15 of 40, 6 of 40 with `STT_MIN_CONFIDENCE=0.5`. It also costs less: 0.26 CPU-seconds per second
of audio against 0.58, and 3.7 GB of GPU memory under load against 4.9 GB. The trade-off is
accuracy on clean wideband speech: 1.3 points worse on LibriSpeech test-clean (5.4 % against
4.1 %, 11 words of 827) and 2.1 points worse on EdAcc South Asian English at 16 kHz (18.4 %
against 16.4 %, 14 words of 678), while on the 8 kHz μ-law audio a phone call actually carries it
is 2.2 points better (18.6 % against 20.8 %, 15 words of 678) and on the noisier LibriSpeech
test-other 1.9 points better (8.1 % against 10.0 %). With 40 clips per set, each of those
differences is within noise; the latency, concurrency and hallucination differences are not. The
160 ms export gains nothing on the final (0.37 s: its 0.6 s Finalize padding costs four chunk
decodes) for 2.7× the CPU and about 3× the GPU work. One condition: the packaged `nemotron-en-560` is an int8
export made for CPUs, and on CUDA it is 8× slower per chunk than full precision (final 0.53 s,
worse than Parakeet), so the server needs the full-precision export registered as its CUDA
variant, as `CUDA_VARIANTS` already does for Parakeet ([below](#what-this-needs-in-the-server)).

## Word error rate (%) through the server

Same sets, normalisation and scoring as [gpu-stt-eval.json](gpu-stt-eval.json), through the
server (pause detection and the Finalize flush included, as in a call); LibriSpeech test-other
is new. Nemotron in full precision on CUDA.

| Set | Parakeet 0.6B v3 | Nemotron 560 ms | Nemotron 160 ms |
|---|---:|---:|---:|
| LibriSpeech test-clean (40 clips, 827 words) | **4.1** | 5.4 | 5.4 |
| LibriSpeech test-other (40 clips, 681 words) | 10.0 | **8.1** | 9.2 |
| EdAcc South Asian English, 16 kHz (40 clips, 678 words) | **16.4** | 18.4 | 17.4 |
| of which L1 Urdu (20) | **18.4** | 20.2 | 19.6 |
| of which L1 Hindi / Indian English (20) | **14.4** | 16.7 | 15.2 |
| EdAcc South Asian, 8 kHz μ-law phone (40 clips) | 20.8 | **18.6** | 18.9 |
| of which L1 Urdu (20) | 23.4 | 20.5 | **20.2** |
| caller lines, 16 kHz (4 lines, 39 words) | 0.0 | 0.0 | 0.0 |
| caller lines, 8 kHz μ-law (4 lines) | 2.6 | **0.0** | **0.0** |

Parakeet's numbers match its earlier runs on the same clips (3.5–4.1 % clean, 14.3–16.5 %
EdAcc, 18.6–20.8 % phone, [deployment-layouts.md](../deployment-layouts.md)). The packaged int8
Nemotron on CUDA was only run on the caller lines (0.0 %).

## Latency, one call at a time

20 clips in real time (the 4 caller lines and 6 EdAcc clips, each at 16 kHz and as 8 kHz μ-law),
`Finalize` 0.2 s after the speech ends; ms, p50 / p90. The int8 column is the packaged export
on CUDA, caller lines only (8 clips).

| | Parakeet 0.6B v3 | Nemotron 560 ms | Nemotron 160 ms | Nemotron 560 ms, int8 on CUDA |
|---|---:|---:|---:|---:|
| **final after the caller stops**, all | 386 / 450 | **257 / 269** | 365 / 377 | 529 / 633 |
| caller lines, 16 kHz | 363 / 443 | **256 / 266** | 364 / 370 | 556 / 611 |
| caller lines, 8 kHz | 302 / 443 | **257 / 269** | 374 / 375 | 484 / 633 |
| EdAcc, 16 kHz | 428 / 450 | **257 / 302** | 350 / 385 | — |
| EdAcc, 8 kHz | 347 / 471 | **256 / 292** | 354 / 385 | — |
| of which after Finalize, all | 175 / 233 | **46 / 48** | 153 / 165 | 318 / 422 |
| live words already read as the final: caller lines, ms after the caller stops | 79 / 238 (5 of 8 lines) | 79 / 198 (**7 of 8**) | 33 / 162 (**7 of 8**) | 266 / 389 (7 of 8) |
| same, all 20 clips (clips where it happened) | 219 / 391 (12) | 12 / 158 (10) | −10 / 122 (11) | — |
| first live words after the caller starts, all | **656 / 1471** | 839 / 1510 | 714 / 1330 | 921 / 1120 |
| server CPU-seconds per second of audio | 0.58 | **0.26** | 0.70 | 1.15 |

Every final was marked `from_finalize`. Nemotron's final is the 0.2 s wait plus about 46 ms
(the 0.6 s Finalize padding is one chunk at 560 ms); Parakeet's is the wait plus a whole-segment
decode. "Live words already read as the final" is the earliest interim result from which every
later interim equalled the final (after normalisation): the moment the voice service could have
acted on the live words. The EdAcc clips end in breath or room sound, so for them that moment
can be before the measured end of speech (negative values); the caller lines, which end cleanly,
are the row to read. Nemotron needs a chunk and its right context before the first words show,
so its first live words come 0.2 s later than Parakeet's.

## Four calls at once

3 rounds of 4 real-time streams starting 0–2 s apart (clips of up to 12 s from LibriSpeech
test-clean and EdAcc at 16 and 8 kHz), 12 finals per model.

| | Parakeet 0.6B v3 | Nemotron 560 ms | Nemotron 160 ms |
|---|---:|---:|---:|
| final after the caller stops, p50 / p90 ms | 448 / 1054 | **259 / 303** | 345 / 405 |
| worst round, p90 ms | 1166 | **314** | 442 |
| errors | 0 | 0 | 0 |
| server CPU cores busy | 1.10 | **0.59** | 1.27 |
| GPU utilisation, mean % | **9** | 14 | 34 |
| GPU memory, max MiB | 4939 | **3693** | 3713 |

Parakeet decodes whole segments, so four finals landing together wait for each other (its
earlier 8-stream run: 0.86 s p50, 1.36 s p90); Nemotron spends about 30 ms per 560 ms chunk as
the audio arrives, so a Finalize finds almost nothing left to do, whatever the other calls are
doing.

## Memory and start-up

| | Parakeet 0.6B v3 | Nemotron 560 ms | Nemotron 160 ms | Nemotron 560 ms, int8 on CUDA |
|---|---:|---:|---:|---:|
| GPU memory after load, MiB | 3453 | 3389 | 3389 | 303 |
| GPU memory, max with 4 streams, MiB | 4939 | 3693 | 3713 | — |
| CPU memory after load / peak, MiB | 1042 / 1446 | 1033 / 1102 | 1005 / 1090 | 1809 / 1830 |
| ready after start, s | 23 | 12 | 12 | 9 |
| model files on disk | 2.4 GB | 2.5 GB | 2.5 GB (shares 2.4 GB with 560 ms) | 0.6 GB |

The int8 export's 303 MiB of GPU memory says where its time goes: its quantised matrix
products run on the CPU (1.15 CPU-seconds per second of audio, 242 ms per 560 ms chunk against
30 ms in full precision).

## Words made up on silence and noise

20 synthetic 4 s clips with no speech (white noise at −75 to −46 dBFS; 1/f noise at −50 to
−32 dBFS; a −60 dBFS floor with one 0.5 s breath-like burst at −40 to −17 dBFS; 1/f noise with
60 Hz hum, two bursts and a click), each through the server at 16 kHz and as 8 kHz μ-law
(Finalize at the end), and the 16 kHz clips straight into each model with no detector in front.

| Clips with text, of 20 | Parakeet 0.6B v3 | Nemotron 560 ms | Nemotron 160 ms |
|---|---:|---:|---:|
| through the server, 16 kHz | 8 | **0** | **0** |
| same with `STT_MIN_CONFIDENCE=0.5` | 3 | **0** | **0** |
| through the server, 8 kHz | 7 | **0** | **0** |
| same with `STT_MIN_CONFIDENCE=0.5` | 3 | **0** | **0** |
| straight into the model (no detector), 16 kHz | 0 | 0 | 0 |

Parakeet's words were all fillers on the breath and line clips (bursts at −33 dBFS and louder):
"Hmm.", "Hm.", "Mm.", "Oh.", "Ah", once "Yeah." (confidence 0.61); the plain noise and
near-silence clips produced nothing. Fed the whole clip it writes nothing either: the words come
from the short segment the voice activity detector cuts around a burst. Nemotron, given the same
segments, stayed silent. The 0.5 filter would have let 3 of 20 through at each rate ("Yeah." 0.61,
"Mm." 0.52, "Hmm." 0.53; at 8 kHz "Hm." 0.56, "Mm." 0.52, "Hmm." 0.51). In the WER passes the
lowest confidence Nemotron reported on real speech was 0.57 (560 ms) and 0.51 (160 ms), Parakeet
0.12, so a 0.5 filter costs Nemotron nothing on these sets.

## Decode cost straight into the model (no server)

- Parakeet 0.6B v3 (full precision, CUDA): a 5 s clip in 50 ms p50 / 68 ms p90 at a length seen
  before, 166 / 205 ms at new lengths (the server pads segments to 500 ms steps for this reason),
  608 ms for the very first decode.
- Nemotron 560 ms (full precision, CUDA): 30 ms per 560 ms chunk (p90 31 ms, max 32 ms over 40
  chunks), 0.047 s of decode per second of audio, Finalize flush 32 ms; the chunk cost does not
  grow with the segment (14 s clip: 28 ms per chunk).
- Nemotron 160 ms (full precision, CUDA): 27 ms per 160 ms chunk, 0.159 s per second of audio,
  flush 106 ms (the 0.6 s padding is four chunks).
- Nemotron 560 ms, packaged int8 export on CUDA: 242 ms per chunk, 0.39 s per second of audio,
  flush 245 ms.

## What this needs in the server

Nothing in `infra/docker/streaming-stt` was changed for this measurement; the notebook registered
the full-precision exports at run time (`fp32_nemotron.py` in the notebook). To ship the
recommendation, `fetch_model.py` needs the two specs below and `CUDA_VARIANTS` entries
`nemotron-en-560 → nemotron-en-560-fp32` and `nemotron-en-160 → nemotron-en-160-fp32`, then
`LOCAL_STT_GPU_MODEL` can default to `nemotron-en-560`. The exports are the sherpa-onnx
maintainers' full-precision conversions of the same model (NVIDIA Open Model License), with the
packaged exports' `tokens.txt`, `decoder.onnx` and `joiner.onnx` shared between the two chunk
sizes; the encoder's weights are in `encoder.data`, which ONNX Runtime reads from the encoder's
directory (as `encoder.weights` for Parakeet):

| | repository (Hugging Face) @ commit | file: SHA-256 |
|---|---|---|
| 560 ms | `csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-560ms-2026-04-25` @ `d655d5f6c5e6a01525a0c905e548facbb1feba5e` | `encoder.onnx`: `9af6849f4f4fbd033469998e7c80bf5f1ac5800e230f20f0ca4b7482a524a7e3` |
| 160 ms | `csukuangfj2/sherpa-onnx-nemotron-speech-streaming-en-0.6b-160ms-2026-04-25` @ `9660703a1289d5b5c0c6470ced80eb52096fc740` | `encoder.onnx`: `b46566b6deb00ad97da0c12bd119ee86e2bc89055c60bf8eba1f2dba205a50ef` |
| both | | `encoder.data` (2.44 GB): `44f65771e1570546f61106b3d0c604a60b398d061476fda8042bb05432601bd4`; `decoder.onnx`: `17614cc0415d49daefd63cd91cb3415af046fa5a487c2049def68f51a667d6fe`; `joiner.onnx`: `d4ef2398e4b19cb166c0c12f03cb20333e4d359d63c5fd8cf965c0649c1d0d03`; `tokens.txt`: `dc0b4584ab2e4ddbf888425c076c61b736e7356a015250db7d307e6f1a8188ff` |

Worth trying with it: `STT_FINALIZE_PAD_MS` below 600 for the 160 ms export (each 160 ms of
padding is one 27 ms chunk), and a turn-end policy in the voice service that acts on live words
that have stopped changing, which Nemotron makes possible (about 0.2 s before Finalize is sent).

## How it was measured

- **The server** is `infra/docker/streaming-stt/app.py` unchanged, installed from
  `requirements.gpu.txt` (sherpa-onnx 1.13.8 CUDA build, ONNX Runtime 1.28.2, CUDA 12.9, cuDNN
  9.10) into a Python 3.12 venv, started as `docker-compose.gpu.yml` starts it:
  `STT_PROVIDER=cuda`, `STT_MAX_SESSIONS=8`, every other setting at its default (2 CPU threads;
  500 ms padding of whole-utterance segments on CUDA; 0.6 s of silence on a Finalize for
  Nemotron; no confidence filter for these models). One server process per model, in turn.
- **The client** sends what the voice service's Deepgram client sends: the same query
  (`linear16`, `interim_results=true`, `endpointing=100`), 20 ms frames in real time, `Finalize`
  0.2 s after the speech ends (the voice service's VAD `stop_secs`), `CloseStream` after the
  final. The WER passes send the audio as fast as the server takes it. The speech end is the
  last 10 ms frame louder than −35 dB below the clip's peak, as `bench_streaming_stt.py` measures
  it. The latency clips: the 4 caller lines and the first 3 Urdu and 3 Indian EdAcc clips, each
  at 16 kHz and as 8 kHz μ-law.
- **Test data**: the notebook output `atlas-stt-eval-data` (the clips of
  [gpu-stt-eval.json](gpu-stt-eval.json): LibriSpeech test-clean, EdAcc South Asian at 16 kHz
  and as 8 kHz μ-law, the caller lines spoken by Pocket TTS), plus LibriSpeech test-other
  (`other/test/0000.parquet` at revision `71cacbfb`, 40 clips of 4–12 s from 33 speakers, one per
  speaker then a second) and the synthetic noise clips, both made in the notebook. Nothing from a
  real caller.
- **Four streams**: `nvidia-smi` sampled every 250 ms; the server process's CPU time from
  `/proc`. **Memory**: `nvidia-smi` on the server's GPU; `VmRSS` / `VmHWM` of the server process.
- **Hallucination through the server**: a clip counts when any final carried text; the
  `STT_MIN_CONFIDENCE=0.5` rows apply that setting to the same finals (the server drops a final
  whose mean token probability is below the setting), so they are what the setting would have
  left.
- The notebook (`atlas-gpu-stt-streaming`, account `abdulmananxx`, private) embeds the server's
  files unchanged and the earlier benchmark helpers; it first ran as a CPU smoke test of every
  code path, so the GPU session spent its 22 minutes on measurements only.

## What is not verified

- **Real callers.** Only synthetic caller lines and EdAcc conversations were used; the earlier
  CPU comparison on recorded callers ([local-models.md](../local-models.md)) had Nemotron level
  with Parakeet 110M on clear English and silent on side talk, which agrees with the
  hallucination result here, but the GPU model was not run on recorded callers.
- **Through the voice service itself.** The client here speaks the same protocol with the same
  timing, but it is not the voice service's pipecat client. The earlier CPU run found that
  client cut Nemotron's last chunk short before the 0.6 s padding existed; the padding is on
  now and every final here was complete, but an end-to-end test call with Nemotron on the GPU
  has not been made.
- **More than 4 calls at once, and the other models on the same GPU.** Parakeet was earlier
  measured with 8 streams next to the language model and the voice (14.6 GB); Nemotron's lower
  memory under load (3.7 GB against 4.9 GB) suggests room, but 8 streams and the full stack
  were not run.
- **Set sizes.** 40 clips per set and 20 latency clips: WER differences of one to two points
  are not reliable (the EdAcc set has 5 speakers); the p90 latencies rest on 2 of 20 clips.
- **Settings not tuned.** `STT_FINALIZE_PAD_MS=600` was kept for both chunk sizes; a smaller
  value for the 160 ms export, and whether 600 is still needed through the voice service's
  client, were not measured.
- **The host's CPU.** 4 vCPUs at 2.0 GHz here; the CPU figures scale with the host.
