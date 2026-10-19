"""Server-Timing, request-id passthrough, gzip, and page totals without a count query."""

from __future__ import annotations

import re

import httpx
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import Receive, Scope, Send

from app.core.middleware import SkipCompressedMediaMiddleware
from tests.conftest import Account

_TIMING = re.compile(r'^app;dur=(?P<app>[\d.]+), db;dur=(?P<db>[\d.]+);desc="(?P<n>\d+) queries"$')


def _timing(resp: httpx.Response) -> tuple[float, float, int]:
    match = _TIMING.match(resp.headers["server-timing"])
    assert match, resp.headers["server-timing"]
    return float(match["app"]), float(match["db"]), int(match["n"])


async def _leads(client: httpx.AsyncClient, acct: Account, n: int) -> None:
    for i in range(n):
        r = await client.post(
            "/api/v1/leads",
            headers=acct.headers,
            json={
                "first_name": f"Lead{i}",
                "company": "Northwind",
                "phone": f"+44 20 7946 02{i:02d}",
            },
        )
        assert r.status_code == 201, r.text


async def test_server_timing_reports_total_and_db(
    client: httpx.AsyncClient, owner: Account
) -> None:
    resp = await client.get("/api/v1/leads", headers=owner.headers)
    assert resp.status_code == 200
    total, db, queries = _timing(resp)
    # Auth (user+session, membership) and the page query at least.
    assert queries >= 3
    assert 0 < db <= total

    live = await client.get("/api/v1/health/live")
    _, db_live, queries_live = _timing(live)
    assert queries_live == 0 and db_live == 0


async def test_server_timing_on_errors(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/leads")
    assert resp.status_code == 401
    assert "server-timing" in resp.headers


async def test_request_id_passthrough(client: httpx.AsyncClient) -> None:
    resp = await client.get("/api/v1/health/live", headers={"X-Request-Id": "trace-abc-12345"})
    assert resp.headers["x-request-id"] == "trace-abc-12345"
    bad = await client.get("/api/v1/health/live", headers={"X-Request-Id": "no spaces allowed!"})
    assert bad.headers["x-request-id"] != "no spaces allowed!"
    assert re.fullmatch(r"[0-9a-f]{32}", bad.headers["x-request-id"])


async def test_large_json_is_gzipped_small_is_not(
    client: httpx.AsyncClient, owner: Account
) -> None:
    await _leads(client, owner, 12)
    headers = {**owner.headers, "Accept-Encoding": "gzip"}
    big = await client.get("/api/v1/leads", headers=headers, params={"page_size": 50})
    assert big.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in big.headers["vary"].lower()
    assert big.json()["total"] == 12  # httpx decodes; shape unchanged

    small = await client.get("/api/v1/health/live", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers

    plain = await client.get(
        "/api/v1/leads",
        headers={**owner.headers, "Accept-Encoding": "identity"},
        params={"page_size": 50},
    )
    assert "content-encoding" not in plain.headers
    assert plain.json() == big.json()


async def test_audio_is_not_gzipped() -> None:
    body = b"RIFF" + b"\x00" * 4096

    async def audio(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"audio/wav"), (b"content-length", b"4100")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def text_app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": b"a" * 4096})

    for inner, expect_gzip in ((audio, False), (text_app, True)):
        app = GZipMiddleware(SkipCompressedMediaMiddleware(inner), minimum_size=1024)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            resp = await c.get("/", headers={"Accept-Encoding": "gzip"})
        encoding = resp.headers.get("content-encoding")
        assert (encoding == "gzip") is expect_gzip, encoding
        if not expect_gzip:
            assert resp.content == body


async def test_totals_are_exact_on_every_page(client: httpx.AsyncClient, owner: Account) -> None:
    await _leads(client, owner, 7)
    url = "/api/v1/leads"
    cases = [
        ({"page": 1, "page_size": 3}, 3),  # full page -> counted
        ({"page": 2, "page_size": 3}, 3),
        ({"page": 3, "page_size": 3}, 1),  # short last page -> derived
        ({"page": 4, "page_size": 3}, 0),  # past the end -> counted
        ({"page": 1, "page_size": 7}, 7),  # exactly one full page -> counted
        ({"page": 1, "page_size": 50}, 7),  # short first page -> derived
        ({"page": 9, "page_size": 50}, 0),
    ]
    for params, n_items in cases:
        body = (await client.get(url, headers=owner.headers, params=params)).json()
        assert body["total"] == 7, params
        assert len(body["items"]) == n_items, params
    none = (await client.get(url, headers=owner.headers, params={"q": "zzz-no-match"})).json()
    assert none == {"items": [], "total": 0, "page": 1, "page_size": 25}
    filtered = (
        await client.get(url, headers=owner.headers, params={"q": "lead1", "page_size": 1})
    ).json()
    assert filtered["total"] == 1  # "Lead1" only: full page of one -> counted


async def test_short_page_skips_the_count_query(client: httpx.AsyncClient, owner: Account) -> None:
    await _leads(client, owner, 4)
    full = await client.get("/api/v1/leads", headers=owner.headers, params={"page_size": 2})
    short = await client.get("/api/v1/leads", headers=owner.headers, params={"page_size": 10})
    assert full.json()["total"] == short.json()["total"] == 4
    assert _timing(full)[2] == _timing(short)[2] + 1


async def test_task_and_campaign_totals(client: httpx.AsyncClient, owner: Account) -> None:
    for i in range(5):
        r = await client.post("/api/v1/tasks", headers=owner.headers, json={"title": f"T{i}"})
        assert r.status_code == 201, r.text
    for page, size, n in ((1, 2, 2), (3, 2, 1), (4, 2, 0), (1, 5, 5), (1, 50, 5)):
        body = (
            await client.get(
                "/api/v1/tasks", headers=owner.headers, params={"page": page, "page_size": size}
            )
        ).json()
        assert (body["total"], len(body["items"])) == (5, n), (page, size)
    ids = [
        t["id"]
        for p in (1, 2, 3)
        for t in (
            await client.get(
                "/api/v1/tasks", headers=owner.headers, params={"page": p, "page_size": 2}
            )
        ).json()["items"]
    ]
    assert len(set(ids)) == 5  # stable order: no repeats or gaps across pages

    empty = (await client.get("/api/v1/campaigns", headers=owner.headers)).json()
    assert empty["total"] == 0 and empty["items"] == []
    calls = (await client.get("/api/v1/calls", headers=owner.headers, params={"page": 2})).json()
    assert calls["total"] == 0
