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
3. A reconciliation job runs every two minutes: live calls still open 90 s after they
   started are re-fetched; calls still open after two hours with no result are closed
   as `failed` / `no_result` (a later real result still replaces that). Simulated calls
   whose completion job was lost are completed by the same job.

### Inbound calls

When the voice service reports a run with no `atlas_call_id` and `direction=inbound`,
ATLAS creates an inbound call for the agent that owns that workflow. The caller number
is taken from the run's initial context (`caller_number`), normalised to E.164 with the
organisation's default country, and matched against live leads. Unknown callers become
a lead (`first_name` "Inbound caller", `source` `inbound_call`, tag `inbound`, default
pipeline stage) with a timeline entry. A withheld or unparseable number still produces
the call record, just without a lead. Assigning phone numbers to agents happens in the
telephony setup, not here.

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
