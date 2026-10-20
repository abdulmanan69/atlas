# Reply latency on the Oracle server

Measured 2026-10-19/20 on the deployed server (Oracle Cloud VM.Standard.E2.4: AMD EPYC
7551, 8 vCPU = 4 cores with 2 threads each, 32 GB) with the automated caller
(`infra/scripts/e2e_test_call.py`) inside the voice service container: 3 to 4 calls of 4
caller lines per configuration, the same caller WAVs every time, 15 s between calls, every
run inside `flock /tmp/atlas-bench.lock`. Raw numbers, methods and all configurations:
[oracle-latency.json](oracle-latency.json).

"Heard" is what the caller perceives: from the caller's last speech frame to the first
agent audio at or above -45 dBFS. Speech-to-text during this work was the organisation's
choice, Whisper small.en (being replaced by another workstream), so it is shown but was not
tuned here.

## Update 2026-10-20: live speech-to-text, turn analyzer, temperature 0.3

Same server and caller lines, after speech-to-text moved to Parakeet TDT 110M (live, local;
[local-stt-real-voice.json](local-stt-real-voice.json)). 3 calls x 4 lines per row, all
passed. Raw summaries: [oracle-turn-settings.json](oracle-turn-settings.json).

| Setting | Reply heard p50 / p90 | Final transcript p50 | Transcript to LLM request p50 | Choppy turns |
|---|---:|---:|---:|---:|
| Balanced (min words 2, 0.6 s speech timeout) | 2.42 / 3.33 s | 672 ms | 397 ms | 1 |
| VAD turn start, 0.6 s speech timeout | 2.37 / 2.71 s | 560 ms | 348 ms | 3 |
| Turn analyzer, 1.0 s fallback | 2.33 / 3.81 s | 650 ms | 112 ms | 2 |
| **Turn analyzer, 1.0 s fallback, temperature 0.3** | **1.94 / 2.10 s** | 562 ms | 124 ms | **0** |

- The turn analyzer (local smart-turn model) ends a finished sentence as soon as the final
  transcript arrives instead of after the 0.6 s speech timeout: 0.3 s less per turn. The
  demo setup now uses it for agents on live speech-to-text (`demo_configure.py`).
- Temperature 0.3 (sent to the model; before, none was sent and the server default applied)
  kept the first sentence short: LLM request to first sentence p90 1.42 s instead of 2.49 s,
  so the slowest tenth of replies came 1.7 s sooner. Replies opened with a kept phrase about
  as often either way (11 of 12 turns with 0.3; 10-12 of 12 in the other rows). With 12
  turns per row, part of the 0.4 s difference in the median may be run-to-run variation.
  The self-hosted presets now set it (`ai_speed.py`, `SELF_HOSTED_LLM_SETTINGS`).
- What is left, p50: 0.56 s final transcript, 0.13 s to the LLM request, **0.95 s to the
  first token** (the model reads about 35 new tokens per turn at 37-40 tokens/s on these
  Zen 1 cores; it writes 18-19 tokens/s), 0.15 s to finish the opener, 0.15 s playout.
  The language model is now the largest part.

(The sections below are the measurements before this update, with Whisper small.en.)

## Result

| | before | after |
|---|---:|---:|
| Reply heard, all turns (p50 / p90) | 7.66 / 9.54 s | **6.44 / 9.42 s** |
| Transcript to reply heard (p50 / p90) | 2.99 / 3.99 s | **1.91 / 2.81 s** |
| First words spoken from memory | 0 of 9 | 9 of 10 |

## Waterfall (p50, ms after the caller stops, clean turns)

| Stage | before | after | What sets it |
|---|---:|---:|---|
| Final transcript (VAD stop 0.2 s + Whisper small.en) | 4,901 | 4,746 | speech-to-text (other workstream) |
| Turn-stop wait | +607 | +608 | hard: 0.6 s speech timeout (below) |
| LLM first token | +973 | +902 | 4 Zen 1 cores, about 25 prompt tokens/s |
| LLM first sentence complete | +395 | +180 | shorter, listed openers |
| TTS first 0.5 s of audio | +706 | **+16** | kept openers (was synthesis + 0.5 s pre-roll) |
| Voice service starts sending | +35 | +29 | |
| Audio reaches the caller (local) | +229 | **+148** | kept openers start 30 ms before their sound (was 80-175 ms) |
| **Heard** | **7,827** | **6,572** | |

## What changed (all measured)

1. **Kept openers in the Pocket TTS adapter** (`infra/docker/pocket-tts/app.py`): 15 short
   openers ("Got it.", "Okay.", "Perfect.", ...) are synthesised at startup per voice and
   kept in memory (bounded, 16 MB; texts up to 32 characters are also kept once spoken).
   Idle benchmark: first 0.5 s of audio in **2.9 ms** instead of 440 ms. A sentence that
   starts with a kept opener ("Got it, perfect timing.", "Great!") is answered with the kept
   opener and then the synthesised rest: first 0.5 s in **5 ms**. Synthesised sentences are
   unchanged (first audio 254 ms, first 0.5 s 524 ms).
2. **Opener rule of the demo agent** (`infra/scripts/demo_configure.py`) lists the kept
   openers. With the rule and the opener matching, 15 of 16 benchmark replies (temperature
   1.0, as the voice service sends) and 9 of 10 clean test-call turns started from memory;
   with the old rule, 4 of 8 benchmark replies began with one of these exact sentences.
3. **LLM threads stay at 4** on this shape, now explicit in
   `infra/deploy/oci/docker-compose.prod.yml`. In isolation 8 threads read prompts 18 %
   faster (first token 0.66 vs 0.77 s), but in calls they slowed speech-to-text, TTS and the
   voice service: LLM first token 2.5 s (all turns) and 2 of 3 calls failed the 15 s limit.
   Pinning the LLM to 3 cores and Pocket TTS to the 4th (compose `cpuset`) was slower
   (first token 1.22 s). 2 threads: 1.47 s alone.

Not changed, with reasons: q4_0 (first sentence 1.15 s) and q8_0 (1.49 s) are slower than
q4_K_M (1.01 s) on this CPU; `qwen2.5:0.5b` is twice as fast (0.53 s) but follows the
prompt worse (invented next steps); `llama3.2:1b` printed tool calls as text and reused no
prompt cache (4.8 s). There is no environment variable for llama-server's batch threads in
this Ollama build. A `num_predict` cap does not shorten the first sentence.

## Hard floors (not configurable without changing the voice service)

| Floor | Cost | Where (image / vendor source) |
|---|---|---|
| VAD end of speech | 0.2 s | `vendor/voice-runtime/api/services/pipecat/run_pipeline.py:181` |
| Turn-stop speech timeout | 0.6 s, measured 607 ms in every turn | `pipecat/turns/user_stop/speech_timeout_user_turn_stop_strategy.py:52`, built with defaults at `run_pipeline.py:259` |
| TTS pre-roll: a sentence plays only after 0.5 s of its audio arrived | about 0.26 s idle, about 0.4 s in calls, 0 for kept phrases | `pipecat/services/tts_service.py:505` (`CHUNK_SECONDS = 0.5`), read at `pipecat/services/speaches/tts.py:89`; `httpx/_decoders.py:228-258` only yields full chunks |
| Sentence end needs the next character | one LLM token, 55-90 ms | `pipecat/utils/text/simple_text_aggregator.py:97-196` |
| Output audio chunk | 40 ms | `pipecat/transports/base_transport.py:79` |

The workflow setting `turn_stop_strategy: turn_analyzer` replaces the 0.6 s timeout with the
local smart-turn model (`run_pipeline.py:247-257`); measured in the update above: 0.3 s less
per turn with live speech-to-text.

## Network

From a PC about 240 ms (TCP round trip) from the server, through the TURN relay, the reply
was heard 0.84 s after the voice service's own start of speech, against 0.26 s for the
caller on the server: **+0.57 s** for distance, about one round trip plus jitter buffering
at both WebRTC ends. Remote reply heard p50 6.96 s.

## Other findings

- After each call the voice service sends a 470-810 token request (only 263-312 cached):
  5.5-8.1 s of all LLM threads, and twice it was cut off at 10 s (HTTP 500). A second call
  starting then would wait for it.
- After a long reply that the caller talked over, the next request re-read 167 tokens
  (first token 5.3 s), because the stored reply no longer matched the cached one.
- When Whisper took over about 6 s, the voice service's idle prompt reached the LLM first
  and was cancelled; llama-server finished that batch before the real request (0.4-1.0 s).
- The model receives temperature 1.0; ATLAS does not expose the temperature the voice
  service supports, which would make small models follow the opener rule more reliably.

## What 0.5 s would take

On this server, with the kept openers, the best case is about **2.3 s** for a local caller
even with a streaming speech-to-text: about 1.1 s from the caller's last word to the LLM
request (0.2 s VAD stop, 0.6 s turn-stop wait and VAD processing, measured with the earlier
streaming speech-to-text), about 1.1 s for the LLM to finish "Got it.", and 0.2 s of TTS and
playout. Reaching 0.5 s needs every stage changed at once:

| Stage | Today | Needed | How |
|---|---:|---:|---|
| End of turn | 1.1 s | ≤ 0.15 s | speech-to-text with built-in end of turn (the voice service's external-turn path, `run_pipeline.py:233-245`) |
| LLM to first sentence | 1.1 s | ≤ 0.15 s | a GPU (prompt reading is then milliseconds) or a hosted low-latency LLM; on CPU about 6-7× this machine's prompt speed (estimate) |
| TTS first audio | 0.02 s kept / 0.5 s synthesised | ≤ 0.05 s | kept openers (done), or a streaming TTS without the 0.5 s pre-roll |
| Playout and network | 0.15 s local, 0.72 s remote | ≤ 0.15 s | caller close to the server region |
