# ADR 0003 — Starting calls and learning their outcome from the voice runtime

**Status:** accepted (design) · 2026-10-18 · implementation in Milestone 4

## Verified runtime facts (source-inspected, runtime API v1.49)
* Auth for machine clients: `X-API-Key` header (keys created via `POST /api/v1/user/api-keys`,
  scoped to one runtime organization).
* An "agent" is a runtime *workflow* (ReactFlow graph of nodes/edges) with versioned,
  published definitions.
* Outbound call: `POST /api/v1/public/agent/workflow/{workflow_uuid}` with
  `{phone_number, initial_context, telephony_configuration_id?, from_phone_number_id?}` →
  `{status, workflow_run_id, workflow_run_name}`. Requires an active, published workflow.
  Returns `429` when the concurrency limit is reached. `initial_context` values are
  available to prompts as `{{key}}` templates.
* Run detail: `GET /api/v1/workflow/{workflow_id}/runs/{run_id}` → completion flag,
  transcript/recording URLs, `usage_info`, `cost_info`, `gathered_context`
  (incl. `call_disposition`).
* Completion notifications exist only as **webhook nodes inside a workflow**, delivered with
  retries and a dead-letter state, but **without an HMAC signature**. Auth is a configured
  credential header.

## Decision
1. When ATLAS provisions an agent in the runtime, it adds a webhook node targeting
   `POST /api/v1/webhooks/voice-runtime`. The node authenticates with a per-deployment
   secret in a custom header (runtime credential type `custom_header`).
2. Every call carries `initial_context.atlas_call_id` (our UUID), so a webhook can be
   matched to our call record without trusting runtime ids alone.
3. The webhook handler **does not trust the payload**. It verifies the header secret in
   constant time, deduplicates on the delivery id, then re-fetches the run from the runtime
   API and uses that response as the authoritative result.
4. A reconciliation job polls runtime runs for calls still open past their expected
   duration, covering lost webhooks and runtime outages.
5. Runtime-specific identifiers stay in adapter-owned columns and are hidden from tenant
   API responses.

## Consequences
* If the runtime is down, campaign dispatch pauses with a visible degraded state. CRM
  functions continue, and missed completions are recovered by reconciliation.
* Signature verification can be upgraded to HMAC if the runtime adds signing; the handler
  is written so the verification strategy is swappable.

## Implementation notes (Milestone 4, verified against the runtime source)

Paths are relative to `vendor/voice-runtime/api/`.

* **Delivery headers.** Every webhook delivery carries a stable delivery id, the run id
  and the attempt number as headers (`tasks/webhook_delivery.py:207-210`). ATLAS
  deduplicates on the delivery id (`webhook_events`, unique per provider); the header
  names live only in `app/integrations/voice_runtime/webhooks.py`. Retries reuse the id
  (`tasks/webhook_delivery.py:368`), so a repeated delivery is acknowledged and ignored.
* **Authentication.** The credential type `custom_header` is sent verbatim as
  `header_name: header_value` (`utils/credential_auth.py:49-52`), resolved at send time
  (`tasks/webhook_delivery.py:185-195`). No HMAC exists; ATLAS compares the per-tenant
  secret with `hmac.compare_digest` and always performs the comparison.
* **Payload template.** The template is rendered with `render_template` against a
  context holding `workflow_run_id`, `workflow_id`, `initial_context`,
  `gathered_context`, `cost_info`, `annotations` and the artifact URLs
  (`tasks/run_integrations.py:383-421`); `call_disposition` is added automatically
  (`tasks/run_integrations.py:439-441`). Missing placeholders render as `""`
  (`utils/template_renderer.py:249-250`), which is how an inbound call arrives with an
  empty `atlas_call_id`. Webhooks fire from the completion task
  (`tasks/workflow_completion.py:59-62`), i.e. after `is_completed` is set.
* **Run detail.** `GET /workflow/{id}/runs/{run_id}` returns `is_completed`,
  `transcript_url` / `recording_url` (storage keys), `public_access_token`,
  `cost_info`, `usage_info` (`llm`, `tts`, `stt`, `call_duration_seconds`),
  `initial_context`, `gathered_context`, `call_type`
  (`routes/workflow.py:1472-1499`, `schemas/workflow.py:9-35`,
  `services/workflow/run_usage_response.py:4-13`). Reconciliation and the webhook
  worker both go through this call.
* **Telephony statuses** mapped onto call states come from `TelephonyCallStatus`
  (`enums.py:37-47`: `no-answer`, `busy`, `failed`, `error`, `canceled`, ...), written
  to `gathered_context.call_status` / `call_disposition` for calls that never
  connected (`services/telephony/status_processor.py:179-202`, which also exposes
  `telephony_error_code` / `telephony_sip_code`).
* **Inbound runs.** Inbound telephony creates the run with `call_type=inbound` and an
  initial context of `caller_number`, `called_number`, `direction="inbound"`,
  `provider`, `telephony_configuration_id` (`routes/telephony.py:497-509`). Outbound
  runs are stamped `direction="outbound"` and `called_number`
  (`routes/telephony.py:196-200, 293-300`). External `initial_context` cannot override
  reserved keys such as `call_id` (`services/workflow/initial_context.py:10-21`), which
  is why ATLAS uses `atlas_call_id`.
* **Artifacts.** Transcripts are stored as `transcripts/{run}.txt`, recordings as
  `recordings/{run}.wav` (`services/workflow_run_artifacts.py:77, 125`).
  `GET /public/download/workflow/{token}/{transcript|recording}` answers `302` to the
  storage URL (`routes/public_download.py:98-118`), which for the local MinIO backend
  is `{MINIO_PUBLIC_ENDPOINT}/{bucket}/{key}` (`services/filesystem/minio.py:122-135`,
  `constants.py:91-96`). ATLAS resolves the redirect with the tenant's runtime
  credentials, then reads the object with a bare client and proxies it; storage URLs
  never reach tenants. Recording upload can be disabled runtime-side
  (`constants.py:131-133`), in which case `recording_url` stays empty.
