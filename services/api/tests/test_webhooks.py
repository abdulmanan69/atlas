"""Inbound call events: secret check, dedupe, authoritative re-fetch, inbound calls."""

from __future__ import annotations

import hmac
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.core.redis import get_redis
from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.webhooks import DELIVERY_ID_HEADER
from app.integrations.voice_runtime.workflow_builder import WEBHOOK_SECRET_HEADER
from app.models.calls import Call, CallEvent
from app.models.crm import Activity, Lead
from app.models.developer import OutboxEvent
from app.models.webhooks import WebhookEvent, WebhookEventStatus
from app.services import jobs
from app.services import webhooks as webhook_service
from app.workers.calls import process_runtime_run
from tests.calls_support import (
    CallsRuntime,
    create_lead,
    enable_live_calls,
    make_runtime,
    published_agent,
    runtime_org,
    start_call,
    webhook_secret,
    workflow_id,
)
from tests.conftest import Account


@pytest.fixture
def runtime() -> Iterator[CallsRuntime]:
    yield from make_runtime()


@pytest.fixture
def live_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_live_calls(monkeypatch)


URL = "/api/v1/webhooks/voice-runtime"


def _headers(secret: str | None, delivery_id: str | None = None) -> dict[str, str]:
    headers = {DELIVERY_ID_HEADER: delivery_id or str(uuid.uuid4())}
    if secret is not None:
        headers[WEBHOOK_SECRET_HEADER] = secret
    return headers


@pytest.fixture
def queue_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unavailable(*_: Any, **__: Any) -> bool:
        raise jobs.QueueUnavailableError("redis down")

    monkeypatch.setattr(jobs, "enqueue", _unavailable)


async def _live_call(
    client: httpx.AsyncClient, owner: Account, fake: CallsRuntime
) -> tuple[dict[str, Any], int]:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call = await start_call(client, owner, lead["id"], agent["id"], live=True)
    assert call["state"] == "dialing" and call["mode"] == "live"
    return call, max(fake.runs)


async def _event_rows() -> list[WebhookEvent]:
    async with get_sessionmaker()() as db:
        return list((await db.execute(select(WebhookEvent))).scalars().all())


@pytest.mark.usefixtures("live_calls")
async def test_secret_is_verified_with_constant_time_compare(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call, run_id = await _live_call(client, owner, runtime)
    org = runtime_org(runtime)
    secret = webhook_secret(org)
    compared: list[tuple[str, str]] = []

    def spy(a: str, b: str) -> bool:
        compared.append((a, b))
        return hmac.compare_digest(a.encode(), b.encode())

    monkeypatch.setattr(webhook_service, "constant_time_equals", spy)
    payload = {"atlas_call_id": call["id"], "workflow_run_id": str(run_id)}

    wrong = await client.post(URL, headers=_headers("nope"), json=payload)
    assert wrong.status_code == 401 and wrong.json()["error"]["code"] == "unauthenticated"
    missing = await client.post(URL, headers=_headers(None), json=payload)
    assert missing.status_code == 401
    # The comparison always runs, against the real secret, so timing stays uniform.
    assert compared == [("nope", secret), ("", secret)]
    assert await _event_rows() == []

    ok = await client.post(URL, headers=_headers(secret), json=payload)
    assert ok.status_code == 202, ok.text
    assert ok.json() == {"status": "accepted"}
    assert secret not in ok.text


async def test_unknown_event_is_not_found_without_detail(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    payload = {"atlas_call_id": str(uuid.uuid4()), "workflow_run_id": "7", "workflow_id": "999"}
    r = await client.post(URL, headers=_headers("anything"), json=payload)
    assert r.status_code == 404
    assert r.json()["error"] == {
        "code": "not_found",
        "message": "Not found",
        "request_id": r.headers["x-request-id"],
    }
    assert (await client.post(URL, headers=_headers(None), json=[1, 2])).status_code == 422
    assert (await client.post(URL, headers=_headers(None), content=b"{not json")).status_code == 422


@pytest.mark.usefixtures("live_calls")
async def test_duplicate_delivery_is_acknowledged_and_ignored(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    call, run_id = await _live_call(client, owner, runtime)
    secret = webhook_secret(runtime_org(runtime))
    payload = {"atlas_call_id": call["id"], "workflow_run_id": str(run_id)}
    delivery = str(uuid.uuid4())

    first = await client.post(URL, headers=_headers(secret, delivery), json=payload)
    assert first.status_code == 202 and first.json()["status"] == "accepted"
    second = await client.post(URL, headers=_headers(secret, delivery), json=payload)
    assert second.status_code == 202 and second.json()["status"] == "duplicate"

    rows = await _event_rows()
    assert len(rows) == 1
    event = rows[0]
    assert event.status == WebhookEventStatus.RECEIVED and event.call_id == uuid.UUID(call["id"])
    assert event.delivery_id == delivery
    # Exactly one job was queued for the worker.
    assert await get_redis().exists(f"arq:job:runtime-event:{event.id}") == 1

    # The worker re-fetches the run and applies it; the event is marked processed.
    runtime.finish(run_id, disposition="callback_requested")
    org = runtime_org(runtime)
    result = await process_runtime_run(
        {"job_try": 1},
        owner.org_id,
        call["id"],
        str(workflow_id(org)),
        str(run_id),
        webhook_event_id=str(event.id),
    )
    assert result == "processed"
    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    assert detail["state"] == "completed" and detail["outcome"] == "callback_requested"
    assert (await _event_rows())[0].status == WebhookEventStatus.PROCESSED


@pytest.mark.usefixtures("live_calls", "queue_down")
async def test_payload_is_never_trusted_runtime_is_authoritative(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    call, run_id = await _live_call(client, owner, runtime)
    secret = webhook_secret(runtime_org(runtime))
    runtime.finish(
        run_id,
        disposition="not_interested",
        extracted={"budget": "none", "opted_out": False},
        transcript="Agent: Hello\nHina: Not interested, thanks.",
    )
    # A forged (but correctly authenticated) body claims a different, better result.
    forged = {
        "atlas_call_id": call["id"],
        "workflow_run_id": str(run_id),
        "workflow_id": "1",
        "call_disposition": "qualified",
        "gathered_context": {"extracted_variables": {"budget": "1M", "opted_out": True}},
        "caller_number": "+15550001111",
    }
    r = await client.post(URL, headers=_headers(secret), json=forged)
    assert r.status_code == 202 and r.json()["status"] == "processed", r.text

    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    assert detail["state"] == "completed" and detail["outcome"] == "not_interested"
    assert detail["captured"] == {"budget": "none"} and detail["opted_out"] is False
    assert detail["duration_seconds"] == 42 and detail["transcript_available"] is True
    assert detail["usage"]["call_duration_seconds"] == 42.0
    assert "workflow" not in r.text and "runtime" not in str(detail).lower()

    event = (await _event_rows())[0]
    assert event.status == WebhookEventStatus.PROCESSED
    assert (
        event.payload["caller_number"] == "[REDACTED]"
        and event.payload["gathered_context"] == "[omitted]"
    )

    events = (await client.get(f"/api/v1/calls/{call['id']}/events", headers=owner.headers)).json()
    assert [e["type"] for e in events] == [
        "created",
        "state_changed",
        "state_changed",
        "result_applied",
    ]
    assert events[-1]["data"]["source"] == "runtime"
    async with get_sessionmaker()() as db:
        outbox = (await db.execute(select(OutboxEvent).order_by(OutboxEvent.id))).scalars().all()
    assert [e.event_type for e in outbox if e.event_type.startswith("call.")] == [
        "call.started",
        "call.completed",
    ]
    assert outbox[-1].data["outcome"] == "not_interested"

    # The lead's do-not-call flag was not flipped by the forged opt-out.
    lead = (await client.get(f"/api/v1/leads/{call['lead_id']}", headers=owner.headers)).json()
    assert lead["do_not_call"] is False and lead["last_contacted_at"] is not None


@pytest.mark.usefixtures("live_calls", "queue_down")
async def test_inbound_call_creates_lead_and_timeline(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    await published_agent(client, owner)
    org = runtime_org(runtime)
    secret = webhook_secret(org)
    wf = workflow_id(org)

    def inbound_run(caller: str) -> int:
        rid = runtime.add_run(
            org.id,
            wf,
            call_type="inbound",
            initial_context={
                "caller_number": caller,
                "called_number": "+14155559999",
                "direction": "inbound",
                "provider": "twilio",
                "telephony_configuration_id": 1,
            },
        )
        runtime.finish(rid, disposition="callback_requested", extracted={"callback_time": "Monday"})
        return rid

    first = inbound_run("+1 (415) 555-0123")
    # Exactly what the runtime renders for an inbound run: atlas_call_id is empty.
    payload = {
        "atlas_call_id": "",
        "workflow_run_id": str(first),
        "workflow_id": str(wf),
        "direction": "inbound",
    }
    r = await client.post(URL, headers=_headers(secret), json=payload)
    assert r.status_code == 202 and r.json()["status"] == "processed", r.text

    calls = (
        await client.get("/api/v1/calls", headers=owner.headers, params={"direction": "inbound"})
    ).json()
    assert calls["total"] == 1
    call = calls["items"][0]
    assert call["mode"] == "live" and call["state"] == "completed"
    assert call["from_phone_e164"] == "+14155550123" and call["to_phone_e164"] == "+14155559999"
    assert call["outcome"] == "callback_requested" and call["lead_id"]

    lead = (await client.get(f"/api/v1/leads/{call['lead_id']}", headers=owner.headers)).json()
    assert lead["phone_e164"] == "+14155550123" and lead["source"] == "inbound_call"
    assert lead["first_name"] == "Inbound caller" and "inbound" in lead["tags"]
    timeline = (
        await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=owner.headers)
    ).json()
    summaries = [a["summary"] for a in timeline["items"]]
    assert "Lead created from an inbound call" in summaries
    assert "Inbound · Call completed · callback_requested" in summaries

    # A second call from the same number matches the existing lead.
    second = inbound_run("+14155550123")
    r = await client.post(
        URL, headers=_headers(secret), json={**payload, "workflow_run_id": str(second)}
    )
    assert r.status_code == 202 and r.json()["status"] == "processed"
    async with get_sessionmaker()() as db:
        assert len((await db.execute(select(Lead))).scalars().all()) == 1
        inbound = (
            (await db.execute(select(Call).where(Call.lead_id == uuid.UUID(lead["id"]))))
            .scalars()
            .all()
        )
        assert len(inbound) == 2
        matched = (
            (await db.execute(select(CallEvent).where(CallEvent.type == "lead_matched")))
            .scalars()
            .all()
        )
        assert sorted(e.data["created"] for e in matched) == [False, True]
    by_lead = (await client.get(f"/api/v1/leads/{lead['id']}/calls", headers=owner.headers)).json()
    assert by_lead["total"] == 2

    # Without a usable caller number the call is still recorded, just without a lead.
    anonymous = runtime.add_run(
        org.id,
        wf,
        call_type="inbound",
        initial_context={"caller_number": "anonymous", "direction": "inbound"},
    )
    runtime.finish(anonymous, disposition=None, call_status="no-answer")
    r = await client.post(
        URL, headers=_headers(secret), json={**payload, "workflow_run_id": str(anonymous)}
    )
    assert r.json()["status"] == "processed"
    async with get_sessionmaker()() as db:
        row = (
            await db.execute(select(Call).where(Call.runtime_run_id == str(anonymous)))
        ).scalar_one()
        assert row.lead_id is None and row.state.value == "no_answer"
        assert len((await db.execute(select(Activity))).scalars().all()) == 3


@pytest.mark.usefixtures("queue_down")
async def test_browser_test_sessions_are_ignored(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    await published_agent(client, owner)
    org = runtime_org(runtime)
    rid = runtime.add_run(
        org.id, workflow_id(org), initial_context={"atlas_test": "true", "atlas_call_id": ""}
    )
    runtime.finish(rid)
    payload = {
        "atlas_call_id": "",
        "workflow_run_id": str(rid),
        "workflow_id": str(workflow_id(org)),
    }
    r = await client.post(URL, headers=_headers(webhook_secret(org)), json=payload)
    assert r.status_code == 202 and r.json()["status"] == "processed"
    assert (await client.get("/api/v1/calls", headers=owner.headers)).json()["total"] == 0
    assert (await _event_rows())[0].status == WebhookEventStatus.PROCESSED


@pytest.mark.usefixtures("live_calls", "queue_down")
async def test_runtime_outage_during_inline_processing_is_recorded(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    call, run_id = await _live_call(client, owner, runtime)
    secret = webhook_secret(runtime_org(runtime))
    runtime.down = True
    r = await client.post(URL, headers=_headers(secret), json={"atlas_call_id": call["id"]})
    # Acknowledged so the sender stops retrying; reconciliation recovers the result.
    assert r.status_code == 202 and r.json()["status"] == "accepted"
    event = (await _event_rows())[0]
    assert event.status == WebhookEventStatus.FAILED and event.error == "voice service unavailable"
    runtime.down = False
    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    assert detail["state"] == "dialing"
