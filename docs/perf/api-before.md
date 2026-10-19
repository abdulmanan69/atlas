# API benchmark - before WS-D

- Date: 2026-10-19 00:45 UTC
- Target: `http://127.0.0.1:8080` (single client, sequential requests, keep-alive)
- Data: fresh org, 300 leads, 60 tasks, 0 calls, 0 campaigns
- Samples: 30 per endpoint after 3 warm-up requests
- Host: Windows 11, Python 3.12.13

| Endpoint | p50 ms | p95 ms | server db p50 ms | body bytes | encoding |
|---|---:|---:|---:|---:|---|
| leads page 1 | 26.1 | 35.4 | n/a | 29274 | identity |
| leads page 5 + search | 23.1 | 33.3 | n/a | 2941 | identity |
| calls | 19.2 | 26.2 | n/a | 46 | identity |
| campaigns | 23.5 | 34.6 | n/a | 46 | identity |
| analytics calls-overview 30d | 30.4 | 38.5 | n/a | 1850 | identity |
| analytics overview (crm) 30d | 30.0 | 40.4 | n/a | 2277 | identity |
| tasks | 45.9 | 172.6 | n/a | 18686 | identity |
