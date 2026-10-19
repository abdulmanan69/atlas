# Calls

A call is one attempted conversation between an AI agent and a person. ATLAS is the
system of record: the `calls` table is written by ATLAS code only, and the voice
service is consulted (never trusted blindly) to learn what happened. There is no
human-transfer feature; every call is handled end to end by an AI agent.

## Lifecycle

```
queued ──▶ dialing ──▶ in_progress ──▶ completed
   │          │             │        ├─▶ no_answer
   │          │             │        ├─▶ busy
   │          └─────────────┴────────┼─▶ canceled
   └─────────────────────────────────┴─▶ failed
```

* `queued` — record created; a simulated call waits here for the worker, a live call
  only momentarily.
* `dialing` — the voice service accepted the outbound call. `in_progress` — the call
  connected (reported by the service, or an inbound call that is still running).
* Terminal states carry `outcome` (one of the agent's configured outcome codes),
  `captured` values, `duration_seconds`, `opted_out`, usage, and whether a transcript
  or recording exists. A failed call carries `error_code` / `error_message`.

Every transition is appended to `call_events` (`created`, `state_changed`,
`result_applied`, `lead_matched`, `transcript_cached`, ...) and is readable at
`GET /calls/{id}/events`. Domain events `call.started` and `call.completed` are emitted
through the transactional outbox for outbound webhooks.

Each completed call writes one entry on the lead's timeline, updates
`last_contacted_at`, and — when the person asked not to be contacted — sets
`do_not_call` and revokes consent, which blocks every future call to that lead.
Browser test calls are the exception (see below): they touch nothing outside their own
record.

## Simulated vs live

* `ATLAS_LIVE_CALLS_ENABLED=false` (default): every call is **simulated**. The record,
  events, timeline entry, outcome distribution and a clearly labelled synthetic
  transcript are produced by the worker about ten seconds later. Nothing leaves the
  platform; `recording_available` is always false.
* `POST /calls` with `live=true` additionally needs the `campaigns:launch_live`
  permission (admin and owner). The agent must be published, the lead must have a valid
  number, must not be on do-not-call and must not have revoked consent — these checks
  apply to simulated calls too.
* Starting a single call needs `campaigns:manage` (manager and above); reading history
  needs `calls:read` (every role); streaming a recording needs `recordings:read`
  (manager and above) and is written to the audit log as `recording.accessed`.

## How results arrive (live calls)

1. The agent's published workflow ends with a report step that calls
   `POST /api/v1/webhooks/voice-runtime` with the shared secret issued when the tenant
   was provisioned. The body only routes the event (`atlas_call_id`, run id, workflow
   id, direction); it is stored redacted in `webhook_events` and never used as a result.
2. The endpoint verifies the secret in constant time, ignores repeated delivery ids,
   answers `202`, and queues `process_runtime_run`, which re-fetches the run from the
   voice service and applies it. If the queue is unavailable the event is processed
   inline within a bounded time.
3. A reconciliation job runs every two minutes: live and browser test calls still open
   90 s after they started are re-fetched; live calls still open after two hours with no
   result are closed as `failed` / `no_result`, test calls still open after 90 minutes
   as `canceled` / `not_completed` (a later real result still replaces either).
   Simulated calls whose completion job was lost are completed by the same job.

### Inbound calls

When the voice service reports a run with no `atlas_call_id` and `direction=inbound`,
ATLAS creates an inbound call for the agent that owns that workflow. The caller number
is taken from the run's initial context (`caller_number`), normalised to E.164 with the
organisation's default country, and matched against live leads. Unknown callers become
a lead (`first_name` "Inbound caller", `source` `inbound_call`, tag `inbound`, default
pipeline stage) with a timeline entry. A withheld or unparseable number still produces
the call record, just without a lead. Assigning phone numbers to agents happens in the
telephony setup, not here.

### Browser test calls

"Start test call" on an agent (`POST /agents/{id}/test-session`) creates a call with
`mode=test`, `direction=inbound` (the person calls in from the browser, which is also
how the voice service records it), the published agent version, the lead whose details
filled the variables (if one was picked), and state `in_progress`. The response carries
its `call_id`. The id travels in the session context as `atlas_call_id`, so the result
arrives exactly like a live call's: the completion webhook (which the voice service
sends for browser sessions too) and the worker fill in the state, duration, transcript,
recording and response times (`latency`, source `runtime`) within seconds of hanging up.

* The browser reports the end with `POST /agents/{id}/test-calls/{call_id}/end`
  (`{"connected": bool}`). A call that never connected (microphone blocked, network)
  is closed at once as `canceled` / `not_connected`; a connected one is re-fetched
  20 s later, which covers deployments where the webhook is not configured.
  Without either signal, reconciliation finishes the call within about 2–4 minutes of
  it ending, or closes it after 90 minutes. A real result always replaces a closure.
* No side effects: no lead timeline entry, `last_contacted_at` and consent are left
  alone (an opt-out said during a test does not mark the lead do-not-call), no
  `call.started` / `call.completed` outbound webhook events, and the call is left out
  of `GET /leads/{id}/calls`. Campaign statistics only count campaign calls, so test
  calls never appear there.
* Analytics: excluded from every figure of `GET /analytics/calls-overview`;
  `mode_split.test` reports how many there were.
* `GET /calls?mode=test` lists them; the web app labels them "Test" and notes on the
  detail page that no phone call was made.

## Transcripts and recordings

* `GET /calls/{id}/transcript` returns the text. For live calls it is fetched from the
  voice service on first request (capped at 2 MB) and stored on the call; later reads
  are served from ATLAS. Simulated calls return their synthetic transcript.
* `GET /calls/{id}/recording` streams the audio through ATLAS with the right content
  type. Storage locations are resolved per request and never exposed. ATLAS must be able
  to reach the storage address the voice service redirects to (local: port 9000).

## Retention

Call records, events and timeline entries are kept for as long as the organisation
exists (`ON DELETE CASCADE`). Transcript text is stored on the call once fetched;
audio stays in the voice service's object storage and is streamed on demand, so
deleting it there makes the recording unavailable (clean 404) without touching the
record. Webhook deliveries are kept in `webhook_events` for support. A retention
policy with automatic purging is not implemented yet.
