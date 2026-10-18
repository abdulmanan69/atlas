# ATLAS Architecture

ATLAS is a multi-tenant platform for running AI voice agents against a CRM: businesses
manage leads, configure agents, launch controlled calling campaigns, and analyse outcomes.

## System context

```
                  ┌──────────────────────────── ATLAS ────────────────────────────┐
 Browser ──HTTPS──▶  web (React SPA)  ──/api/v1──▶  api (FastAPI)  ──▶ PostgreSQL │
                  │                                   │   ▲             (system of │
                  │                                   │   │              record)   │
                  │                          enqueue  ▼   │ results                │
                  │                             Redis ◀──▶ worker (ARQ)            │
                  │                                         │                      │
                  └─────────────────────────────────────────┼──────────────────────┘
                                                            │ adapter (HTTP, X-API-Key)
                                                            ▼
                                       Voice runtime (internal service, headless)
                                       ├─ real-time STT → LLM → TTS pipeline
                                       ├─ telephony: Twilio / Vonage / Plivo / Telnyx / SIP
                                       └─ providers: hosted APIs or local (OpenAI-compatible,
                                          Whisper, local TTS)
```

* **web** — React + TypeScript SPA. Talks only to `/api/v1`. Never sees DB credentials or
  provider secrets.
* **api** — FastAPI modular monolith. Owns tenancy, auth, CRM, agents, campaigns, call
  records, analytics, integrations, audit.
* **worker** — ARQ process sharing the api codebase. Runs campaign dispatch, webhook
  processing, runtime reconciliation, retries. Nothing long-running happens in a request.
* **PostgreSQL** — system of record. **Redis** — job queue, rate limiting, short-lived
  coordination. Losing Redis degrades the platform; it does not lose business data.
* **Voice runtime** — the open-source real-time voice engine (see `THIRD_PARTY_NOTICES.md`),
  deployed as an internal service. It is an *integration*, not the product: ATLAS keeps
  its own records and keeps working when the runtime is unavailable.

## Backend layering

| Layer | Location | Responsibility |
|---|---|---|
| Routes | `app/api/v1/endpoints/` | Transport, validation, auth dependencies, response mapping |
| Dependencies | `app/api/v1/deps.py` | DB session, current user, tenant context, permission aliases |
| Services | `app/services/` | Business rules and orchestration, transactions, audit |
| Models | `app/models/` | SQLAlchemy tables and enums |
| Schemas | `app/schemas/` | Pydantic request/response contracts (drive OpenAPI) |
| Integrations | `app/integrations/` | Voice runtime, LLM/STT/TTS, telephony, storage adapters |
| Workers | `app/workers/` | Queued and scheduled jobs |

Repositories are not a separate layer yet: services issue SQLAlchemy queries directly
because every query is tenant-filtered in one obvious place. A repository layer will be
introduced only where a query is reused across services.

## Tenancy and authorization

* Every tenant-owned table has `organization_id` (FK, `ON DELETE CASCADE`, indexed).
* The client sends `X-Organization-Id`; `get_tenant` verifies an **active membership** for
  the authenticated user on every request. Unknown and foreign organizations return the same
  `403` (no tenant enumeration).
* Every service query filters by `ctx.org_id`. Cross-tenant ids resolve to `404`.
* Roles: `owner > admin > manager > agent > viewer`, permissions in
  `app/core/permissions.py`. Endpoints declare one typed alias, e.g. `ctx: LeadsWrite`.
  Nobody can grant or manage a role above their own; the last owner cannot be removed.
* PostgreSQL row-level security is planned as defence in depth (ADR 0002), not a substitute.

## Data model (current)

`organizations`, `users`, `memberships`, `auth_sessions`, `invitations`,
`pipeline_stages`, `leads`, `activities`, `tasks`, `audit_logs`.

Key invariants enforced by the database:

* One live lead per normalized phone per organization (partial unique index).
* Case-insensitive unique user email; one pending invitation per email per org.
* Enumerations stored as constrained `VARCHAR` (adding a value needs no type migration).
* Lead search uses a persisted generated column with a trigram GIN index.
* Leads are soft-deleted (`deleted_at`) so call and audit history keeps its references.

Planned (Milestones 4–7): `agents`, `agent_versions`, `campaigns`, `campaign_leads`,
`calls`, `call_events`, `integrations`, `webhook_events`, `usage_records`.

## Cross-cutting concerns

* **Errors** — one envelope: `{"error": {code, message, request_id, details}}`.
  Unhandled errors log a stack trace server-side and return a generic `500`.
* **Correlation** — `X-Request-ID` accepted or generated, echoed in responses, attached to
  every log line and audit entry.
* **Logging** — JSON to stdout; keys like `password`, `token`, `authorization` are redacted.
* **Security headers**, body-size limit (10 MB), strict CORS allow-list.
* **Health** — `/health/live` (process only), `/health/ready` (DB required; Redis and worker
  reported as degraded, not fatal).
* **Configuration** — `ATLAS_*` environment variables (`app/core/config.py`). Non-local
  environments refuse to boot with default secrets.

## Calling safety

`ATLAS_LIVE_CALLS_ENABLED=false` by default. While false, no code path can place a real call;
campaigns run in dry-run/simulation using an explicitly named mock adapter. Live campaigns
additionally require the `campaigns:launch_live` permission, consent, and DNC checks.
