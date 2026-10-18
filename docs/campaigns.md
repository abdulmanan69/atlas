# Campaigns

A campaign calls an audience of leads with a published AI agent, under explicit pacing,
calling-hour and retry rules. ATLAS keeps every call as its own record; the campaign
only decides *who* is called *when*.

## Lifecycle

```
draft ──start──▶ scheduled ──(start_at reached)──▶ running ──▶ completed
  │                  │  ▲                           │  ▲
  │                  │  └──────── resume ───────────┤  │ resume
  │                  ▼                              ▼  │
  └──cancel──▶   canceled ◀────── cancel ────────  paused ──▶ failed
```

| From | To |
|---|---|
| draft | scheduled, running, canceled |
| scheduled | running, paused, canceled, failed |
| running | paused, completed, canceled, failed |
| paused | scheduled, running, canceled, failed |
| completed / canceled / failed | — (terminal) |

Every move goes through `transition()` in `app/models/campaigns.py`; anything else is a
`409 invalid_state_transition`.

* **start** validates the agent (published and active), snapshots the audience into
  `campaign_leads` and moves to `running`, or to `scheduled` when `start_at` is in the
  future. Starting an already running campaign is a no-op.
* **pause** stops dispatching; calls already in progress finish normally.
* **resume** re-validates the agent and the live gates, then continues.
* **cancel** marks pending leads `skipped (campaign_canceled)`; in-flight calls are still
  recorded when they finish.
* The worker moves a running campaign to **completed** when no lead is pending or
  calling, and **pauses** it (with `last_error`) when the agent is unpublished or live
  calls are switched off mid-run, so a person can fix the cause and resume.

## Safety gates

Nothing in this module places a call directly. Every attempt goes through
`app.services.calls.start_outbound_call`, which enforces do-not-call, revoked consent,
missing phone numbers and unpublished agents for *every* call, live or simulated.

A **dry run** (`mode: dry_run`, the default) only ever creates simulated calls: no audio,
no telephony, synthetic outcomes clearly labelled `[Simulated]` on the lead timeline.

A **live** campaign starts only when **both** hold:

1. the deployment sets `ATLAS_LIVE_CALLS_ENABLED=true`, and
2. the person starting (or resuming) it holds `campaigns:launch_live` (admin and owner).

Either missing gives `403`. The authorising user is written to the audit log
(`campaign.live_authorized`) and stored as `started_by_user_id`. If the flag is turned off
while a live campaign runs, the worker pauses it before the next call.

Other limits, all enforced by the worker from database state:

* **Calling hours** per weekday, evaluated in the lead's timezone, falling back to the
  campaign's `timezone`, then the organization's. Leads outside their window are parked
  until their next opening, so they never block others.
* **Max concurrency** (1–50): calls for the campaign that are queued, dialing or in
  progress.
* **Calls per minute** (1–60): calls created in the last 60 seconds.
* **Max calls** (optional usage limit): once reached, remaining leads are
  `skipped (usage_limit_reached)` and the campaign completes.
* **Start/end window**: nothing is dispatched after `end_at`; remaining leads become
  `skipped (campaign_window_ended)`.
* **Runtime capacity**: a 429 from the voice service leaves the lead pending and backs the
  whole campaign off for 60 seconds (`backoff_until`).

## Audience

The audience is a saved lead filter (same keys as the leads list: `q`, `stage_id`,
`owner_user_id`, `unassigned`, `tag`, `qualification_status`, `do_not_call`, `source`,
`created_from`, `created_to`) and/or an explicit list of lead ids; when both are given the
union is used. Archived leads and other tenants' leads never match.

`POST /campaigns/{id}/preview` reports, without side effects, how many leads match, how
many would be called, and how many would be skipped by reason (`do_not_call`,
`consent_revoked`, `no_phone`), plus a rough duration and whether the calling window is
open right now.

On start the audience is **snapshotted** into `campaign_leads` (one row per lead,
`skipped` rows carry the reason). Leads added later do not join a running campaign; the
call core still re-checks each lead at dial time, so a lead that opts out mid-campaign is
skipped.

## Retries

`retry_policy` = `{max_attempts: 1–5, retry_on: [no_answer | busy | failed], backoff_minutes}`.

| Call result | Campaign lead |
|---|---|
| completed (answered) | `completed`, `last_outcome` = the agent's outcome code |
| in `retry_on`, attempts left | `pending`, due in `backoff_minutes × attempts` |
| in `retry_on`, no attempts left | `exhausted` |
| any other failure | `failed` |

A person who opts out during a call is marked do-not-call by the call core; any later
attempt is refused and recorded as `skipped (do_not_call)`.

## Scheduler

Two cron jobs in `app/workers/campaigns.py`, every 30 seconds, offset from each other.
They hold no state in memory: a worker restart, or several workers at once, change nothing.

* `dispatch_campaigns` locks one campaign at a time (`FOR UPDATE SKIP LOCKED`), computes the
  free capacity, claims due leads with row locks, and calls the call core. Each placement
  runs in a savepoint so a refused call leaves no half-written record. For simulated calls
  it asks the call core (`calls.schedule_simulated_completion`) to finish the call after a
  random 5–20 s delay; if the queue is down, the call core's reconciliation finishes it.
* `advance_campaign_leads` maps finished calls onto their leads per the retry policy,
  recovers claims whose call record disappeared, gives up on leads stuck in `calling` for
  six hours, closes campaigns with nothing left to do, emits `campaign.completed`, and
  refreshes the cached `stats`.

## What is simulated

In a dry run the agent, audience, pacing, calling hours, retries, call records, lead
timeline entries and analytics are all real. Only the conversation is not: the outcome is
drawn deterministically from the agent's outcome codes (`app/integrations/voice_runtime/simulated.py`)
and no phone number is dialled.

## API

`GET/POST /campaigns`, `GET/PATCH/DELETE /campaigns/{id}`, `POST …/preview`, `…/start`,
`…/pause`, `…/resume`, `…/cancel`, `GET …/progress`, `GET …/leads`. Reading needs
`campaigns:read`; everything else `campaigns:manage`; live start/resume additionally
`campaigns:launch_live`.
