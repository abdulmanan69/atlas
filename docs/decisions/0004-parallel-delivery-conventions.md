# ADR 0004 — Conventions that let features be built in parallel

**Status:** accepted · 2026-10-18

## Context
Milestones 4–7 (calls, campaigns, telephony, actions, knowledge, integrations, flow
canvas, reports) are developed by several engineers/agents at the same time in one
repository. Shared registration files and a shared test database were the main sources
of conflicts.

## Decisions
1. **Auto-discovery instead of registration files.** `app/models/__init__.py` imports every
   module in `app/models/`; `app/api/v1/router.py` mounts every `router` found in
   `app/api/v1/endpoints/`. Adding a domain never edits a shared file.
2. **Models-first test schema.** `ATLAS_TEST_SCHEMA=models` makes the test suite build the
   schema from the SQLAlchemy models (`create_all`) instead of migrations, so a feature can
   be tested before its migration exists. CI always uses migrations. One consolidated
   migration is generated when parallel work lands, then `alembic check` must pass.
3. **One test database per workstream.** `ATLAS_TEST_DATABASE_URL` / `ATLAS_TEST_REDIS_URL`
   select an isolated database (`atlas_test_a`, `_b`, …) and Redis logical DB, because the
   suite truncates tables between tests.
4. **Frontend types from code, not from a running server.** `pnpm gen:api` exports the
   OpenAPI document with `python -m app.openapi_export` and generates types from the file.
5. **Shared call core.** `app/services/calls.py` is the only module that decides whether a
   real call is placed (`start_outbound_call`, honouring `ATLAS_LIVE_CALLS_ENABLED`) and
   the only module that maps runtime results onto call records (`apply_runtime_result`).
   Campaigns, webhooks and reconciliation call into it rather than duplicating it.
6. **Ownership by directory.** Each workstream owns its models/services/endpoints/tests
   modules and feature folder; cross-stream changes go through the owner.
