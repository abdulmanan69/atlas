"""Benchmark the hot read endpoints of the ATLAS API.

Signs up a throwaway user + organization through the public auth flow, seeds leads and
tasks through the API, then measures client-side latency (p50/p95) for the list and
analytics endpoints the web app hits on every page load. Prints a Markdown table.

    cd services/api
    uv run python ../../infra/scripts/bench_api.py --base http://127.0.0.1:8080 \
        --out ../../docs/perf/api-after.md

Only stdlib + httpx. Numbers are wall-clock on whatever machine runs the script, against
whatever server is listening, so compare runs made on the same machine back to back.
"""

from __future__ import annotations

import argparse
import platform
import sys
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx

API = "/api/v1"
FIRST = ["Ayesha", "Bilal", "Sara", "Omar", "Hina", "Usman", "Zara", "Ali", "Noor", "Imran"]
LAST = ["Khan", "Ahmed", "Malik", "Hussain", "Raza", "Shah", "Iqbal", "Butt", "Qureshi", "Javed"]
COMPANIES = ["Example Co", "Northwind", "Contoso", "Globex", "Initech", "Umbrella", "Hooli"]
SOURCES = ["web_form", "csv_import", "referral", "ads", None]


def percentile(samples: list[float], pct: float) -> float:
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    k = (len(ordered) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def signup(client: httpx.Client) -> tuple[str, str]:
    tag = uuid.uuid4().hex[:10]
    resp = client.post(
        f"{API}/auth/signup",
        json={
            "email": f"bench-{tag}@example.com",
            "password": f"bench-passphrase-{tag}",
            "full_name": "Bench User",
            "organization_name": f"Bench Org {tag}",
        },
    )
    resp.raise_for_status()
    body = resp.json()
    return body["access_token"], body["memberships"][0]["organization"]["id"]


def seed(client: httpx.Client, leads: int, tasks: int) -> list[str]:
    ids: list[str] = []
    start = time.perf_counter()
    for i in range(leads):
        payload = {
            "first_name": FIRST[i % len(FIRST)],
            "last_name": f"{LAST[(i // len(FIRST)) % len(LAST)]} {i}",
            "phone": f"+1 415 555 {i:04d}",
            "email": f"lead{i}@example.com",
            "company": COMPANIES[i % len(COMPANIES)],
            "source": SOURCES[i % len(SOURCES)],
            "tags": ["bench", "q4" if i % 2 else "inbound"],
        }
        resp = client.post(f"{API}/leads", json=payload)
        if resp.status_code == 422:  # phone rejected by the normalizer: seed without one
            payload.pop("phone")
            resp = client.post(f"{API}/leads", json=payload)
        resp.raise_for_status()
        ids.append(resp.json()["id"])
    due = datetime.now(UTC)
    for i in range(tasks):
        client.post(
            f"{API}/tasks",
            json={
                "title": f"Follow up {i}",
                "lead_id": ids[i % len(ids)],
                "due_at": (due + timedelta(hours=i - tasks // 2)).isoformat(),
            },
        ).raise_for_status()
    print(
        f"seeded {leads} leads + {tasks} tasks in {time.perf_counter() - start:.1f}s",
        file=sys.stderr,
    )
    return ids


ENDPOINTS: list[tuple[str, str]] = [
    ("leads page 1", f"{API}/leads?page=1&page_size=50"),
    ("leads page 5 + search", f"{API}/leads?page=5&page_size=5&q=khan"),
    ("calls", f"{API}/calls?page=1&page_size=50"),
    ("campaigns", f"{API}/campaigns?page=1&page_size=25"),
    ("analytics calls-overview 30d", f"{API}/analytics/calls-overview?range_days=30"),
    ("analytics overview (crm) 30d", f"{API}/analytics/crm-overview?range_days=30"),
    ("tasks", f"{API}/tasks?page=1&page_size=50"),
]


def measure(client: httpx.Client, url: str, n: int, warmup: int) -> dict[str, object]:
    samples: list[float] = []
    db_ms: list[float] = []
    queries = None
    size = 0
    encoding = "identity"
    for i in range(warmup + n):
        t0 = time.perf_counter()
        resp = client.get(url)
        elapsed = (time.perf_counter() - t0) * 1000
        resp.raise_for_status()
        if i < warmup:
            continue
        samples.append(elapsed)
        size = int(resp.headers.get("content-length") or len(resp.content))
        encoding = resp.headers.get("content-encoding", "identity")
        for part in resp.headers.get("server-timing", "").split(","):
            name, _, rest = part.strip().partition(";")
            if name == "db" and "dur=" in rest:
                db_ms.append(float(rest.split("dur=")[1].split(";")[0]))
                if 'desc="' in rest:
                    queries = int(rest.split('desc="')[1].split()[0])
    return {
        "p50": percentile(samples, 50),
        "p95": percentile(samples, 95),
        "db_p50": percentile(db_ms, 50) if db_ms else None,
        "queries": queries,
        "bytes": size,
        "encoding": encoding,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="http://127.0.0.1:8080")
    parser.add_argument("--requests", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    parser.add_argument("--leads", type=int, default=300)
    parser.add_argument("--tasks", type=int, default=60)
    parser.add_argument("--label", default="")
    parser.add_argument("--out", help="also write the Markdown report to this file")
    args = parser.parse_args()

    with httpx.Client(base_url=args.base, timeout=30.0) as client:
        token, org_id = signup(client)
        client.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": org_id,
                "Accept-Encoding": "gzip",
            }
        )
        seed(client, args.leads, args.tasks)
        rows = [(name, measure(client, url, args.requests, args.warmup)) for name, url in ENDPOINTS]

    lines = [
        f"# API benchmark{' - ' + args.label if args.label else ''}",
        "",
        f"- Date: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Target: `{args.base}` (single client, sequential requests, keep-alive)",
        f"- Data: fresh org, {args.leads} leads, {args.tasks} tasks, 0 calls, 0 campaigns",
        f"- Samples: {args.requests} per endpoint after {args.warmup} warm-up requests",
        f"- Host: {platform.system()} {platform.release()}, Python {platform.python_version()}",
        "",
        "| Endpoint | p50 ms | p95 ms | server db p50 ms | SQL queries | body bytes | encoding |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    for name, r in rows:
        db = f"{r['db_p50']:.1f}" if r["db_p50"] is not None else "n/a"
        nq = r["queries"] if r["queries"] is not None else "n/a"
        lines.append(
            f"| {name} | {r['p50']:.1f} | {r['p95']:.1f} | {db} | {nq} | {r['bytes']} "
            f"| {r['encoding']} |"
        )
    report = "\n".join(lines) + "\n"
    print(report)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
