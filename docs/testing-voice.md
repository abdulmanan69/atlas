# Testing the voice stack end to end

`infra/scripts/e2e_test_call.py` places a real test call to a published agent with no
person and no microphone, then reports how long each reply took **as the caller hears
it** next to what the voice service measured itself. Use it after changing models, call
settings, the voice service or the machine, and to get honest latency numbers.

It follows the same path as **Test call** in the agent editor:

```
ATLAS API: sign in -> POST /agents/{id}/test-session
  -> voice service signalling (WebSocket, session token, Origin = web app)
  -> WebRTC audio both ways (Opus, 20 ms frames)
  -> voice pipeline: speech-to-text -> language model -> voice (the organization's models)
```

The script plays the browser's part. A scripted caller (four short lines a mortgage lead
might say) is synthesised by the local Pocket TTS server in a different voice (`marius`)
from the agent's (`alba`), or read from WAV files you supply.

## What happens during a call

1. Sign in (`POST /auth/login`), pick the organization (`--org`, else the first one that
   has the agent), find the agent by name (default **Mortgage lead qualifier**) and create
   a test session.
2. Open the signalling socket with the session token, send a WebRTC offer, wait for the
   answer and for the audio connection. The caller sends silence meanwhile.
3. Wait for the agent's greeting and for it to finish.
4. For each caller line: wait until the voice service no longer mutes the caller, send
   the line in real time, then wait for the agent's reply to start and to finish.
5. Hang up like the browser: close the audio connection, then the signalling socket. When
   ATLAS recorded the test call, report its end (`POST /agents/{id}/test-calls/{call_id}/end`).
6. Fetch the voice service's own per-turn record: from ATLAS
   (`GET /calls/{call_id}/latency`) when the test call is recorded there, otherwise from the
   voice service's database (the run's `logs.realtime_feedback_events`, see
   [ADR 0006](decisions/0006-low-latency-voice.md)). Print both views side by side.

The voice service also pushes its events live over the signalling socket (what it heard,
what it said, its measured response time, first-byte times per stage). Those are saved to
`events.jsonl` and shown as the "live" value, so a comparison is available even when the
stored record is not.

## Before you run it

- Voice service running: `make voice-up` (`.\make.ps1 voice-up`).
- ATLAS API on port 8080 and the worker (`make dev-api`, `make worker`, or the demo setup).
- The agent published, and the organization's model selection set (the demo uses
  self-hosted Whisper, qwen2.5:1.5b and Pocket TTS).
- Self-hosted models running: `docker compose --profile local-ai up -d local-llm local-speech local-tts`.
  They need about 2.5 GB of RAM together; on a small machine stop them afterwards with
  `make local-ai-down`.
- The demo password in `.env` (`ATLAS_DEMO_PASSWORD`), or `--password`. It is never printed.

## Run it

```bash
make e2e-call                                         # Linux / macOS
make e2e-call ARGS="--runs 2 --json docs/perf/e2e-test-call.json"
```

```powershell
.\make.ps1 e2e-call                                   # Windows
.\make.ps1 e2e-call --runs 2 --json docs/perf/e2e-test-call.json
```

The script itself only needs the Python standard library. The WebRTC part needs `aiortc`;
when it is not installed where you run the script, the script copies itself into the
`voice-runtime` container (which has it), runs there and copies the results back. That is
also the reliable way on Windows and macOS: Docker Desktop does not route WebRTC's UDP
from the host to a container, so a host-side client would need a TURN relay. Force a mode
with `--runner local` or `--runner docker`.

Useful options (all have defaults; `--help` lists everything):

| Option | Default | Meaning |
|---|---|---|
| `--agent` | Mortgage lead qualifier | Agent to call (must be published) |
| `--org` | first membership | Organization name, slug or id |
| `--utterance TEXT` | 4 mortgage-lead lines | Caller lines, repeat the option |
| `--utterance-wav FILE` | | Use recorded 16-bit WAV files instead of synthesised lines |
| `--tts-voice` | marius | Caller voice on the Pocket TTS server |
| `--runs N` | 1 | Calls placed one after another |
| `--max-reply-ms` | 15000 | Fail when a reply starts later than this |
| `--json FILE` | | Also write the full result here |
| `--results-dir` | `.cache/e2e-test-call/` | Recordings and result files (git-ignored) |

## Reading the results

Each run prints a header line and a table:

```
### Run 1 - PASS
call ... | voice run 412 | connected in 180 ms | greeting 2310 ms after connect, 6.2 s | hang-up by caller, ...

| Turn | Caller said | Reply heard after (client) | Voice service response (stored / live) | STT / LLM / TTS first byte | Agent spoke | Gaps > 200 ms | Voice service heard |
```

- **Reply heard after (client)**: from the end of the caller's speech (the last speech
  sample sent) to the first agent audio received. This is the gap a person on the call
  feels, minus the last few tens of milliseconds of playback buffering in a real browser.
  `(overlap)` means the agent was still talking while the caller spoke.
- **Voice service response**: the voice service's own number for the same turn, measured
  from when *its* voice detector decided the caller had stopped to when its bot started
  speaking. Expect it to be lower than the client number: the detector waits 0.2 s of
  silence before deciding, and the client number also includes encoding, the network and
  the jitter buffer. A large difference between the two (more than about half a second)
  points at transport or an overloaded voice service, not at the models.
- **STT / LLM / TTS first byte**: time to first result per stage, from the voice service.
  On CPU-only machines the language model's first token is usually the largest part.
- **Agent spoke**: from the first to the last agent speech of the turn.
- **Gaps > 200 ms**: silences inside one agent reply, by kind:
  - `pause`: low-level sound between words or sentences, part of the voice itself. Normal.
  - `silent`: the gap holds more than 200 ms of exact digital silence. The voice service
    sends zeros when it has nothing queued to play, so the next sentence was not
    synthesised in time. Audible as choppy.
  - `break`: the agent went quiet for longer than `--end-silence-ms` and then carried on
    in the same turn. Choppy, and on a real call the person would likely start talking.
  - `stall(s)`: no audio packets arrived for that long (network, or a voice service too
    busy to send). Choppy.
  A turn with a `silent` or `break` gap, or a stall, over 200 ms is marked **CHOPPY**.
- **Voice service heard**: what speech-to-text made of the caller's line. If it is wrong,
  the reply will be too; check the caller audio first.

Below the table the script lists what the agent said in each turn, then transport counters
(packets received, the longest pause between packets, lost audio).

**The agent's words come from the live events, not the stored record.** The voice
service's saved event log pairs each reply's text with another reply's timestamps, so in
the stored per-turn record (and in ATLAS's response-times view, which reads it) a turn's
agent text is often the previous or next reply, or empty. The stored response times and
the caller's text are correct. The live text is taken from the same time window as the
reply, so it is the one shown. In `result.json` both are kept: `server.agent_text`
(stored) and `server_live.agent_text` (live).

### How the agent's speech is detected

A plain energy detector on the received audio, per 10-20 ms frame:

| Setting | Default | Rule |
|---|---|---|
| `--vad-db` | -45 dBFS | A frame at or above this RMS level is speech |
| `--min-speech-ms` | 60 | Speech starts at the first loud frame once 60 ms of loud audio fall within 300 ms (clicks are ignored) |
| `--end-silence-ms` | 1500 | Speech ends at the last loud frame before 1.5 s of quiet |
| `--gap-ms` | 200 | Quiet runs and packet pauses longer than this inside speech are listed |
| `--silence-db` | -90 dBFS | Below this is the voice service's filler (exact zeros); a gap with more than `--gap-ms` of it is `silent`. Natural pauses in the voice never reached exact zeros in the measured calls |

The agent counts as finished when the detector has closed its speech **and** the voice
service no longer reports the bot as speaking (the second condition is dropped 3 s later
in case that event never arrives). The caller's own lines are trimmed of leading and
trailing silence with the same `--vad-db` threshold.

### Pass or fail

The exit code is 0 when every run passed, 1 when a run failed, 2 for a setup problem
(sign-in, agent not found or not published, caller voice unavailable). A run fails when:

- the audio connection could not be established, or the voice service reported an error;
- the greeting never arrived (`--greeting-timeout-s`, 60 s);
- a reply never started (`--reply-timeout-s`, 45 s) or started later than `--max-reply-ms`.

Choppy audio is reported but does not fail the run.

### Files

Under `--results-dir/<date-time>/` (git-ignored):

| File | Contents |
|---|---|
| `result.json` | Everything below the table, per run and summarised (the same as `--json`) |
| `run-N/agent.wav` | The agent's audio exactly as received (48 kHz mono) |
| `run-N/conversation.wav` | Both sides on one timeline, caller left, agent right (16 kHz) |
| `run-N/events.jsonl` | The voice service's live events with arrival times |
| `caller-audio/*.wav` | The synthesised caller lines; reuse them with `--utterance-wav` |

In `result.json`, each turn has `reply_ms` (client), `server` (the stored per-turn record,
when found) and `server_live` (from the live events). `runtime_view.source` says where the
stored record came from: `atlas`, `voice-db`, or `null` with `notes` explaining why not.

## Troubleshooting

- **`signalling connection failed ... 403`**: the session token expired or the Origin does
  not match the web app origin the session was created for. Pass `--origin` with the value
  of `ATLAS_WEB_APP_ORIGIN`.
- **`WebRTC did not connect`** with `--runner local` on Windows or macOS: run it in the
  container (the default when aiortc is not installed on the host), or provide TURN.
- **The greeting never arrived**: check the agent's first node greets, the model
  connections work (AI providers page), and the voice service log (`make voice-logs`).
- **Replies time out on CPU models**: the first call after the models start includes
  loading them; run `make local-ai-warm` first, or look at the LLM first-byte column.
- **`voice service database not reachable`**: only matters for the stored view when ATLAS
  does not record test calls; the live values are still shown.

## Measured on the development machine

Two calls on 2026-10-19, a 6-core CPU without a GPU, all models self-hosted (Whisper,
qwen2.5:1.5b, Pocket TTS), full output in [perf/e2e-test-call.json](perf/e2e-test-call.json).
Run 1 began 84 s after the model containers had been restarted (cold); run 2 followed it.

| Turn | Run 1 heard after (client / voice service) | Run 2 heard after (client / voice service) | Run 2 LLM first token |
|---|---|---|---|
| Greeting (after connect) | 3.4 s | 2.1 s | |
| 1 | 25.4 s / 25.1 s | 4.0 s / 3.6 s | 0.6 s |
| 2 | 17.1 s / 16.8 s | 5.5 s / 5.4 s | 1.3 s |
| 3 | 10.8 s / 10.6 s | 4.4 s / 4.2 s | 1.0 s |
| 4 | 21.8 s / 21.6 s | 10.1 s / 9.8 s | 4.8 s |
| Result | fail (3 replies over 15 s) | pass | |

- The client heard each reply 165-425 ms after the voice service's own measurement, on
  every turn; no audio was lost and packets never paused for more than 111 ms. On this
  machine the delay is in the models, not in transport.
- Speech-to-text mostly took 1.2-1.9 s per turn on CPU and the voice's first audio
  0.5-0.9 s; the language model's first token varied most (0.6-11.9 s).
- 4 of 8 turns were choppy: a mid-reply gap of 0.8-1.3 s holding 0.3-0.8 s of exact
  silence (the next sentence not synthesised in time), or a 1.6-2.9 s break inside one
  reply while the model was still writing the next sentence.
- Earlier the same day, with other jobs loading the CPU, replies took 12.8-38.0 s (LLM
  first token 20-23 s). When the model containers were restarted during a call, the voice
  service ended it after the greeting and the test failed, as it should.
