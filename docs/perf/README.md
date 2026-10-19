# API and web performance (ADR 0006, WS-D)

Measured with `infra/scripts/bench_api.py`:

```sh
cd services/api
uv run python ../../infra/scripts/bench_api.py --base http://127.0.0.1:8080 --out ../../docs/perf/api-after.md
```

The script signs up a new user and organization through `POST /api/v1/auth/signup`, seeds
300 leads and 60 tasks through the API, then sends 3 warm-up requests and 30 measured
requests to each endpoint, one at a time, from one keep-alive client with
`Accept-Encoding: gzip`. Latency is measured on the client. The "server db" column is
read from the new `Server-Timing` header, so it is empty in the "before" run.

Raw reports: [api-before.md](api-before.md), [api-after.md](api-after.md).

## Before and after

Dev machine (Windows 11, uvicorn with one worker and no reload, Postgres 17 and Redis in
Docker Desktop/WSL2, about 1 GB of free RAM). Both runs used the same dev database,
one right after the other on 2026-10-19.

| Endpoint | p50 before | p50 after | p95 before | p95 after | SQL queries after | body before → after |
|---|---:|---:|---:|---:|---:|---|
| leads page 1 (50 rows) | 26.1 | 22.5 | 35.4 | 25.1 | 4 | 29,274 B → 3,363 B gzip |
| leads page 5 + search `q=khan` | 23.1 | 17.9 | 33.3 | 21.5 | 4 | 2,941 B → 711 B gzip |
| calls (empty org) | 19.2 | 14.0 | 26.2 | 15.6 | 3 (count skipped) | 46 B |
| campaigns (empty org) | 23.5 | 14.5 | 34.6 | 17.8 | 3 (count skipped) | 46 B |
| analytics calls-overview 30d | 30.4 | 32.7 | 38.5 | 41.6 | 10 | 1,850 B → 373 B gzip |
| analytics crm-overview 30d | 30.0 | 24.6 | 40.4 | 29.5 | 7 | 2,277 B → 666 B gzip |
| tasks (60 rows, page of 50) | 45.9 | 18.6 | 172.6 | 19.6 | 4 | 18,686 B → 3,261 B gzip |

All times are in ms.

### How much to trust these numbers

- **These are small differences on a noisy machine.** The first "before" run, against a
  cold server right after Docker started, gave p50s of 20–155 ms and p95s up to 470 ms. That
  run was discarded and the warm repeat is the one recorded. Two "after" runs differed by
  up to 3 ms at p50 and 8 ms at p95 (crm-overview p95: 29.5 vs 37.1). Read differences
  under about 5 ms as noise.
- **calls-overview is not a like-for-like comparison.** Between the two runs, another
  workstream (call latency, ADR 0006) added three queries to that endpoint (10 now, 7
  before). Its time went up for that reason, not because of this work.
- **Most of each request is fixed overhead, not SQL.** The database executes every one of
  these queries in under 1 ms (`EXPLAIN ANALYZE`, below). The "server db" column (5–15
  ms) is mostly round trips: about 1.7 ms per query from Windows to Postgres inside
  WSL2. Every authenticated request runs 2 queries for auth (user + session, then
  membership) before its own work.
- The dataset is small: 300 leads per org, 640 leads in the whole database, 0 calls and
  0 campaigns in the bench org. At this size the planner uses sequential scans no matter
  which indexes exist, so the new indexes do not show up in these numbers. They were
  checked on a larger copy of the data instead (below).

## What changed

### API

1. **`Server-Timing` on every response**: `app;dur=<total ms>, db;dur=<ms>;desc="<n> queries"`.
   SQLAlchemy `before/after_cursor_execute` events (`app/core/timing.py`, attached in
   `app/db/session.py`) add up statement time into a per-request contextvar that
   `RequestContextMiddleware` opens. CORS now exposes the header. `X-Request-ID` was
   already passed through when valid (8–64 characters from `[A-Za-z0-9._-]`); tests now
   cover it.
2. **Gzip**: `GZipMiddleware(minimum_size=1024, compresslevel=6)`. Level 6 is used instead
   of Starlette's default 9: level 9 usually makes JSON only slightly smaller and costs
   more CPU. That tradeoff was not measured here. `SkipCompressedMediaMiddleware` marks `audio/*`, `video/*` and `image/*` responses
   `Content-Encoding: identity` so call recordings are streamed as they are. JSON list
   pages shrink 4–9×.
3. **No count query when the page answers it** (`app/db/pagination.py`). When a page comes
   back short (fewer rows than `page_size`, and not an empty page past the end), the
   total is `offset + rows`. Otherwise one `COUNT(*)` runs on the filtered query with its
   `ORDER BY` removed. Used by leads, lead timeline, calls, campaigns, campaign leads and
   tasks. This saves one round trip on most small or filtered lists and on the last page
   of any list. Response shapes are unchanged.
4. **Campaign progress counters are scoped to the organization.** `compute_stats` filtered
   calls by `campaign_id` only, and no index starts with that column. It now also filters
   by `organization_id`, so it uses `(organization_id, campaign_id, state)`. Measured on a
   copy with 96k calls: 15.5 ms with a sequential scan before, 3.9 ms with a bitmap index
   scan after.
5. **Stable task order**: `Task.id` breaks ties after `due_at, created_at`, so offset pages
   never repeat or skip a task.
6. **One migration, `4e2c93ca9274_list_query_indexes`**, with composite indexes that match
   how each query filters and sorts:

   | Table | Index | Serves |
   |---|---|---|
   | leads | `(organization_id, updated_at)` | "recently updated" sort |
   | leads | `(organization_id, last_contacted_at)` | "last contacted" sort |
   | calls | `(organization_id, lead_id, created_at)` | a lead's call history; replaces `(org, lead)` |
   | calls | `(organization_id, campaign_id, state)` | campaign progress, calls filtered by campaign; replaces `(org, campaign)` |
   | campaigns | `(organization_id, created_at)` | campaign list |
   | campaign_leads | `(campaign_id, created_at)` | a campaign's lead list (the campaign is checked against the tenant first) |
   | tasks | `(organization_id, status, due_at)` | open and overdue counters, task list filtered by status |

   Activities were already covered by `ix_activities_lead_time (organization_id, lead_id,
   occurred_at)`. Leads already had `(org, created_at)`, `(org, stage)`, `(org, owner)`,
   `(org, next_follow_up_at)` and trigram search.

   Checked on a copy with 96k tasks and 96k calls, inside a transaction that was rolled
   back: the open-tasks count uses a bitmap scan on `ix_tasks_org_status_due` (1.4 ms).
   The status-filtered task page uses an index scan with an incremental sort (0.24 ms).

   The indexes are created without `CONCURRENTLY` because the tables are still small. On
   a large production table, create them concurrently before applying this migration.

### Web

1. **Every page route is lazy-loaded already.** All routes in `router.tsx` use `lazy:`.
   Nothing changed there.
2. **Prefetch on hover and focus** (`src/app/prefetch.ts`, wired into the sidebar in
   `app-shell.tsx`). Hovering a link for 60 ms, or focusing it with the keyboard, loads
   that route's code and its first query for Leads, Calls, Campaigns, Reports and Agents.
   The query keys and fetchers match what each page asks for on its first render, so
   after the click the page reads from the cache. `prefetch.test.ts` checks that the keys
   and page sizes stay in sync with the pages.
3. **Reference data stays fresh for 5 minutes**: members and agents (it was 60 s; stages
   were already 5 minutes). Every write to these already invalidates the cache.
4. **AG Grid**: `maxBlocksInCache` went from 2 to 10 on the leads and calls grids. That is
   500 rows in memory, so scrolling back up no longer refetches blocks that were just
   dropped. `cacheBlockSize` stays at 50 so the first page stays small.
5. **Main chunk**: `index-*.js` went from 49.37 kB (17.43 kB gzip) to 55.07 kB (18.87 kB
   gzip), **+1.44 kB gzip**, under the 5 kB budget. The query-key module now loads in the
   main chunk because the sidebar needs it.

## Next steps (not done here)

- Auth costs 2 round trips per request (user + session, then membership). Merging them
  into one join, or caching the membership briefly in Redis, would save about 2 ms on
  every call on this machine.
- calls-overview runs 8 analytics queries. A few of them could be combined into one pass
  over `calls`.
