# ADR 0001 — Modular monolith, with the voice runtime behind an adapter

**Status:** accepted · 2026-10-18

## Context
The open-source voice runtime we build on is itself a complete application: its own
FastAPI backend, Next.js UI, PostgreSQL, Redis, object storage, auth and campaign engine.
We need enterprise CRM, tenancy and analytics on top, under our own brand, and the ability
to upgrade (or replace) the runtime without rewriting the product.

## Options
1. **Fork and extend the runtime directly** — add CRM tables and screens inside it.
   Fast start, but every upstream release becomes a merge conflict, its branding appears in
   hundreds of UI strings, and our product is coupled to its data model.
2. **Separate product, runtime as an internal service (chosen)** — ATLAS owns the user
   experience and system of record; it drives the runtime through its documented HTTP API.
3. Microservices from day one — unnecessary operational cost for current scale.

## Decision
Option 2, implemented as a **modular monolith** (one API codebase + one worker process).
The runtime runs headless on the internal network; customers never see its UI or name.
All runtime access goes through `app/integrations/voice_runtime/`, which maps runtime
payloads to internal domain models and IDs.

## Consequences
* Upstream upgrades are a version bump plus adapter contract tests.
* Two databases exist (ours and the runtime's). Ours is authoritative for business data;
  the runtime's is treated as an external system and reconciled.
* Higher baseline memory than a single app — documented in `docs/deployment.md`.
* The BSD-2 copyright notice of the runtime is retained in `THIRD_PARTY_NOTICES.md`.
