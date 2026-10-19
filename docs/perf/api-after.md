# API benchmark - after WS-D

- Date: 2026-10-19 01:07 UTC
- Target: `http://127.0.0.1:8080` (single client, sequential requests, keep-alive)
- Data: fresh org, 300 leads, 60 tasks, 0 calls, 0 campaigns
- Samples: 30 per endpoint after 3 warm-up requests
- Host: Windows 11, Python 3.12.13

| Endpoint | p50 ms | p95 ms | server db p50 ms | SQL queries | body bytes | encoding |
|---|---:|---:|---:|---:|---:|---|
| leads page 1 | 22.5 | 25.1 | 7.8 | 4 | 3363 | gzip |
| leads page 5 + search | 17.9 | 21.5 | 7.0 | 4 | 711 | gzip |
| calls | 14.0 | 15.6 | 5.1 | 3 | 46 | identity |
| campaigns | 14.5 | 17.8 | 5.2 | 3 | 46 | identity |
| analytics calls-overview 30d | 32.7 | 41.6 | 14.9 | 10 | 373 | gzip |
| analytics overview (crm) 30d | 24.6 | 29.5 | 12.2 | 7 | 666 | gzip |
| tasks | 18.6 | 19.6 | 7.2 | 4 | 3261 | gzip |
