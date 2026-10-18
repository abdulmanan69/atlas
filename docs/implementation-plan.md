# Implementation Plan

Status legend: ✅ done · 🟡 in progress · ⬜ not started

## Milestone 1 — Foundation ✅
- ✅ Repo layout, `.gitignore`, `.env.example`, `THIRD_PARTY_NOTICES.md`
- ✅ Python 3.12 + uv lockfile, FastAPI app factory, settings with production guards
- ✅ Docker Compose: PostgreSQL 17, Redis 7 (localhost-only ports, memory limits)
- ✅ Alembic (async) with naming conventions; `alembic check` drift test
- ✅ JSON logging, request IDs, security headers, body limit, error envelope
- ✅ Liveness/readiness with DB, Redis, worker heartbeat
- ✅ ARQ worker skeleton, Makefile + `make.ps1`, GitHub Actions CI

## Milestone 2 — Authentication and CRM ✅ (backend)
- ✅ Sign-up, login, Argon2 hashing, lockout after 5 failures, rate limits
- ✅ JWT access (15 min) + rotating opaque refresh cookie with reuse detection
- ✅ Sessions list/revoke, logout
- ✅ Organizations, memberships, invitations, role hierarchy, last-owner protection
- ✅ Leads CRUD, E.164 normalization, per-tenant dedup, search/filter/sort/paginate
- ✅ Pipeline stages, stage transitions with timeline entries, notes, tasks
- ✅ Bulk actions, CSV import (dry-run + report), streaming CSV export (formula-safe)
- ✅ Audit log; 44 tests against PostgreSQL incl. tenant isolation and RBAC

## Milestone 3 — Enterprise frontend ✅ (CRM scope)
- ✅ React 19 + Vite 7 + Tailwind 4 + Radix, strict TypeScript, types generated from OpenAPI
- ✅ Silent token refresh (single-flight), tenant-scoped query cache cleared on org switch/sign-out
- ✅ Sign in / sign up / accept invitation; app shell with org switcher, Ctrl+K search, themes
- ✅ Overview dashboard from `/analytics/crm-overview`; leads table (URL-state filters, sort,
  pagination, row selection, bulk assign/stage/tag/archive); lead detail with stage control,
  timeline, notes, inline owner/consent/DNC/follow-up; CSV import with dry-run report; export
- ✅ Tasks, team & invitations, organization settings, sessions, audit log
- ✅ 19 component/unit tests; production build with vendor chunking (app entry ≈33 kB)
- ⬜ Agent, campaign, call-history and integrations screens — built with their APIs in M4–M5

## Milestone 4 — Voice runtime integration ✅
- ✅ Runtime runs headless in Docker (own DB/Redis/storage), pinned to the version whose
  source is contract-checked; telemetry off; vendor name nowhere user-facing
- ✅ Per-tenant runtime accounts provisioned lazily (own API key, secrets encrypted at rest)
- ✅ AI providers: catalog, connections (keys verified live, never echoed), model selection,
  provider check, voice catalog; vendor-hosted bootstrap models hidden and retired
- ✅ Agents: guided editor, versions, publish (runtime-validated), lifecycle, browser test
  calls (WebRTC, no API key in the browser)
- ✅ Calls: authenticated + deduplicated completion webhook with authoritative re-fetch,
  inbound-call lead matching, reconciliation cron, simulated calls, proxied recordings
  and transcripts, call events; 17 tests
- ✅ Live contract tests against the real runtime (`ATLAS_TEST_RUNTIME_URL`)

## Milestone 5 — Campaigns ✅ · 5b flow canvas ✅
- ✅ Campaign state machine, audience snapshot with skip reasons, preview, calling hours
  per lead timezone, concurrency + per-minute pacing, retries/exhaustion, usage limit,
  restart-safe dispatcher (`FOR UPDATE SKIP LOCKED`), dry-run default, live launch gated by
  `ATLAS_LIVE_CALLS_ENABLED` **and** `campaigns:launch_live` with audit; 64 tests
- ✅ Campaign screens: list, 5-step wizard (audience preview, calling hours, pacing,
  review with LIVE confirmation), detail with controls, progress meters, leads and outcomes
- ✅ Calls screens: filterable grid, "Call now", call detail with transcript, recording
  player, captured data, events; demo seed adds a finished dry run with simulated calls
- ✅ Flow canvas (ADR 0005): guided→flow conversion (lossless, versioned), ATLAS-side
  validation stricter than the runtime (reachability, dangling steps, path-name collisions)
  with runtime errors mapped back to ATLAS node/path ids, call settings tab, React Flow
  editor (custom Start/Step/End nodes, path inspector, dagre tidy-up, undo/redo, minimap,
  keyboard shortcuts, global rules panel);
  canvas seeds from the saved version so conversions and version switches render
  immediately; 24 web tests

## Milestone 6 — AI providers ⬜
Hosted providers first (configured through the runtime), then local inference
(OpenAI-compatible LLM endpoint, Whisper STT, local TTS) with health checks and a setup
guide sized for CPU-only hardware.

## Milestone 7 — Analytics and integrations ✅
- ✅ Tenant API keys (hashed, role-capped, rate-limited, audited) with `X-API-Key` auth
- ✅ Outbound webhooks: transactional outbox, HMAC-signed deliveries, retries with
  backoff, dead-letter, auto-disable, DNS-rebinding-safe destination policy; 39 tests
- ✅ Call analytics (`/analytics/calls-overview`, precise null-vs-zero metrics) + Reports
  screen (KPIs with definitions, daily chart, outcomes, by agent/campaign, CSV export)
- ✅ Phone numbers (providers, numbers, inbound agent per number, console setup
  instructions), Actions (HTTP tools with shared destination policy), Knowledge
  (uploads via server, status, search, external-processing disclosure, kill switch)
- ✅ Screens for phone numbers (vendor-managed providers hidden, reasons scrubbed), actions,
  knowledge, API keys & webhooks (custom Radix controls throughout, no native selects)

## Milestone 8 — Hardening ⬜
Full check suite, security review, E2E flow (org → lead → agent → dry-run campaign →
simulated call → timeline → analytics → isolation check), known-limitations doc.
