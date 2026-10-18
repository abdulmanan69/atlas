"""Calls: starting, simulated completion, history, transcripts, recordings, reconciliation."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from app.core.redis import get_redis
from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.simulated import SimulatedResult
from app.models.audit import AuditLog
from app.models.calls import Call, CallState
from app.models.developer import OutboxEvent
from app.services import calls as call_service
from app.workers.calls import complete_simulated_call, reconcile_calls
from tests.calls_support import (
    CallsRuntime,
    create_lead,
    enable_live_calls,
    make_runtime,
    published_agent,
    start_call,
)
from tests.conftest import Account, invite_and_join, signup


@pytest.fixture
def runtime() -> Iterator[CallsRuntime]:
    yield from make_runtime()


@pytest.fixture
def live_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    enable_live_calls(monkeypatch)


def _simulate(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> None:
    base: dict[str, Any] = {
        "answered": True,
        "outcome": "qualified",
        "duration_seconds": 61,
        "captured": {"budget": "synthetic"},
        "transcript": "[SIMULATED CALL]\nAgent: Hi Hina\nHina: (synthetic reply)",
        "opted_out": False,
    }
    result = SimulatedResult(**{**base, **overrides})
    monkeypatch.setattr(call_service, "simulate", lambda **_: result)


async def _audit(action: str) -> list[AuditLog]:
    async with get_sessionmaker()() as db:
        return list((await db.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


async def test_simulated_call_completes_and_writes_timeline(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    _simulate(monkeypatch)
    call = await start_call(client, owner, lead["id"], agent["id"])
    assert call["mode"] == "simulated" and call["state"] == "queued"
    assert call["transcript_available"] is False and call["recording_available"] is False
    assert runtime.count("POST", r"^/public/agent/") == 0, "nothing left the platform"
    assert await get_redis().exists(f"arq:job:simulated-call:{call['id']}") == 1
    assert len(await _audit("call.started")) == 1

    assert await complete_simulated_call({}, call["id"]) == "completed"
    assert await complete_simulated_call({}, call["id"]) == "completed", "idempotent"

    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    assert detail["state"] == "completed" and detail["outcome"] == "qualified"
    assert detail["duration_seconds"] == 61 and detail["captured"] == {"budget": "synthetic"}
    assert detail["transcript_available"] is True and detail["started_at"] < detail["ended_at"]
    assert "runtime" not in str(detail).lower() and "workflow" not in str(detail).lower()

    transcript = (
        await client.get(f"/api/v1/calls/{call['id']}/transcript", headers=owner.headers)
    ).json()
    assert transcript["simulated"] is True and transcript["text"].startswith("[SIMULATED CALL]")
    assert runtime.count("GET", r"^/public/download/") == 0

    events = (await client.get(f"/api/v1/calls/{call['id']}/events", headers=owner.headers)).json()
    assert [e["type"] for e in events] == ["created", "state_changed", "result_applied"]
    assert events[1]["data"] == {"from": "queued", "to": "completed", "reason": "simulated"}

    timeline = (
        await client.get(f"/api/v1/leads/{lead['id']}/activities", headers=owner.headers)
    ).json()
    entry = next(a for a in timeline["items"] if a["type"] == "call")
    assert entry["summary"] == "[Simulated] Call completed · qualified"
    assert entry["actor_type"] == "system" and entry["data"]["call_id"] == call["id"]
    lead_after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=owner.headers)).json()
    assert lead_after["last_contacted_at"] is not None and lead_after["do_not_call"] is False
    async with get_sessionmaker()() as db:
        outbox = (await db.execute(select(OutboxEvent).order_by(OutboxEvent.id))).scalars().all()
    call_events = [e for e in outbox if e.event_type.startswith("call.")]
    assert [e.event_type for e in call_events] == ["call.started", "call.completed"]
    assert call_events[1].data["mode"] == "simulated"
    assert call_events[1].data["outcome"] == "qualified"


async def test_simulated_opt_out_sets_do_not_call(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    _simulate(monkeypatch, outcome="do_not_call", opted_out=True)
    call = await start_call(client, owner, lead["id"], agent["id"])
    await complete_simulated_call({}, call["id"])
    lead_after = (await client.get(f"/api/v1/leads/{lead['id']}", headers=owner.headers)).json()
    assert lead_after["do_not_call"] is True and lead_after["consent_status"] == "revoked"
    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    assert detail["opted_out"] is True and detail["outcome"] == "do_not_call"
    # The lead can no longer be called, simulated or not.
    r = await client.post(
        "/api/v1/calls",
        headers=owner.headers,
        json={"lead_id": lead["id"], "agent_id": agent["id"]},
    )
    assert r.status_code == 422 and r.json()["error"]["code"] == "call_not_allowed"


async def test_live_calls_are_blocked_unless_enabled_and_permitted(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    body = {"lead_id": lead["id"], "agent_id": agent["id"], "live": True}
    # ATLAS_LIVE_CALLS_ENABLED=false (the default): nobody can place a real call.
    r = await client.post("/api/v1/calls", headers=owner.headers, json=body)
    assert r.status_code == 403 and "ATLAS_LIVE_CALLS_ENABLED" in r.json()["error"]["message"]
    assert runtime.count("POST", r"^/public/agent/") == 0
    assert (await client.get("/api/v1/calls", headers=owner.headers)).json()["total"] == 0

    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    rep = await invite_and_join(client, owner, "rep@example.com", "agent")
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    monkeypatch.setattr(call_service.get_settings(), "live_calls_enabled", True)
    # Managers may start simulated calls but lack campaigns:launch_live.
    assert (
        await client.post("/api/v1/calls", headers=manager.headers, json=body)
    ).status_code == 403
    assert (
        await client.post("/api/v1/calls", headers=manager.headers, json={**body, "live": False})
    ).status_code == 201
    for member in (rep, viewer):
        assert (
            await client.post("/api/v1/calls", headers=member.headers, json={**body, "live": False})
        ).status_code == 403
    assert runtime.count("POST", r"^/public/agent/") == 0


@pytest.mark.usefixtures("live_calls")
async def test_live_call_is_placed_through_the_runtime(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner, company="Northwind")
    call = await start_call(client, owner, lead["id"], agent["id"], live=True)
    assert call["mode"] == "live" and call["state"] == "dialing" and call["started_at"]
    assert call["to_phone_e164"] == "+14155550100"
    sent = runtime.last("POST", r"^/public/agent/workflow/")
    assert sent["phone_number"] == "+14155550100"
    assert sent["initial_context"]["atlas_call_id"] == call["id"]
    assert (
        sent["initial_context"]["first_name"] == "Hina"
        and sent["initial_context"]["company"] == "Northwind"
    )
    assert "runtime" not in str(call).lower() and "workflow" not in str(call).lower()
    assert await get_redis().exists(f"arq:job:simulated-call:{call['id']}") == 0

    runtime.capacity_full = True
    r = await client.post(
        "/api/v1/calls",
        headers=owner.headers,
        json={"lead_id": lead["id"], "agent_id": agent["id"], "live": True},
    )
    assert r.status_code == 429 and r.json()["error"]["code"] == "voice_capacity_reached"
    assert (await client.get("/api/v1/calls", headers=owner.headers)).json()["total"] == 1


async def test_history_filters_sorting_and_lead_calls(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await published_agent(client, owner)
    first = await create_lead(client, owner, phone="+14155550101")
    second = await create_lead(client, owner, phone="+14155550102")
    _simulate(monkeypatch, outcome="qualified")
    a = await start_call(client, owner, first["id"], agent["id"])
    await complete_simulated_call({}, a["id"])
    _simulate(
        monkeypatch, answered=False, outcome=None, duration_seconds=0, transcript="", captured={}
    )
    b = await start_call(client, owner, second["id"], agent["id"])
    await complete_simulated_call({}, b["id"])
    c = await start_call(client, owner, second["id"], agent["id"])  # still queued

    base = "/api/v1/calls"

    async def ids(**params: Any) -> list[str]:
        r = await client.get(base, headers=owner.headers, params=params)
        assert r.status_code == 200, r.text
        return [i["id"] for i in r.json()["items"]]

    assert await ids() == [c["id"], b["id"], a["id"]], "newest first"
    assert await ids(lead_id=second["id"]) == [c["id"], b["id"]]
    assert await ids(state="completed") == [a["id"]]
    assert await ids(state="no_answer") == [b["id"]]
    assert await ids(outcome="qualified") == [a["id"]]
    assert await ids(mode="simulated", direction="outbound", agent_id=agent["id"]) == [
        c["id"],
        b["id"],
        a["id"],
    ]
    assert await ids(direction="inbound") == []
    assert await ids(campaign_id=str(uuid.uuid4())) == []
    now = datetime.now(UTC)
    assert await ids(created_from=(now - timedelta(minutes=5)).isoformat()) == [
        c["id"],
        b["id"],
        a["id"],
    ]
    assert await ids(created_to=(now - timedelta(minutes=5)).isoformat()) == []
    page = (
        await client.get(base, headers=owner.headers, params={"page": 2, "page_size": 2})
    ).json()
    assert page["total"] == 3 and [i["id"] for i in page["items"]] == [a["id"]]

    by_lead = (
        await client.get(f"/api/v1/leads/{second['id']}/calls", headers=owner.headers)
    ).json()
    assert [i["id"] for i in by_lead["items"]] == [c["id"], b["id"]]
    assert (
        await client.get(f"/api/v1/leads/{uuid.uuid4()}/calls", headers=owner.headers)
    ).status_code == 404
    # Archiving a lead keeps its call history reachable.
    assert (
        await client.delete(f"/api/v1/leads/{second['id']}", headers=owner.headers)
    ).status_code == 204
    assert (await client.get(f"/api/v1/leads/{second['id']}/calls", headers=owner.headers)).json()[
        "total"
    ] == 2
    # No transcript for an unanswered call.
    assert (
        await client.get(f"{base}/{b['id']}/transcript", headers=owner.headers)
    ).status_code == 404


@pytest.mark.usefixtures("live_calls")
async def test_recording_is_proxied_authorized_and_audited(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call = await start_call(client, owner, lead["id"], agent["id"], live=True)
    run_id = max(runtime.runs)
    url = f"/api/v1/calls/{call['id']}/recording"
    assert (await client.get(url, headers=owner.headers)).status_code == 404, "no result yet"

    audio = b"RIFF" + bytes(range(256)) * 300
    runtime.finish(run_id, recording=audio, transcript="Agent: Hello")
    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(call["id"]))
        assert row is not None
        await call_service.sync_from_runtime(db, row)
        await db.commit()
    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    assert detail["recording_available"] is True and detail["state"] == "completed"

    r = await client.get(url, headers=owner.headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav" and r.content == audio
    assert r.headers["cache-control"] == "private, no-store"
    assert r.headers["content-disposition"] == f'inline; filename="call-{call["id"]}.wav"'
    assert "storage.test" not in r.text[:0] + str(r.headers)
    accessed = await _audit("recording.accessed")
    assert len(accessed) == 1 and accessed[0].resource_id == call["id"]
    assert accessed[0].actor_user_id == uuid.UUID(owner.user_id)

    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    rep = await invite_and_join(client, owner, "rep@example.com", "agent")
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    assert (await client.get(url, headers=viewer.headers)).status_code == 403
    assert (await client.get(url, headers=rep.headers)).status_code == 403
    assert (await client.get(url, headers=manager.headers)).status_code == 200
    # Viewers can still read the call record itself.
    assert (
        await client.get(f"/api/v1/calls/{call['id']}", headers=viewer.headers)
    ).status_code == 200
    assert len(await _audit("recording.accessed")) == 2

    # The runtime's copy disappeared: a clean 404, not a broken stream.
    runtime.objects.clear()
    assert (await client.get(url, headers=owner.headers)).status_code == 404


@pytest.mark.usefixtures("live_calls")
async def test_transcript_is_fetched_once_then_stored(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call = await start_call(client, owner, lead["id"], agent["id"], live=True)
    run_id = max(runtime.runs)
    url = f"/api/v1/calls/{call['id']}/transcript"
    assert (await client.get(url, headers=owner.headers)).status_code == 404

    runtime.finish(run_id, transcript="Agent: Hi Hina\nHina: Hello — yes, still interested.")
    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(call["id"]))
        assert row is not None
        await call_service.sync_from_runtime(db, row)
        await db.commit()

    first = await client.get(url, headers=owner.headers)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "call_id": call["id"],
        "mode": "live",
        "simulated": False,
        "text": "Agent: Hi Hina\nHina: Hello — yes, still interested.",
    }
    second = await client.get(url, headers=owner.headers)
    assert second.json() == first.json()
    assert runtime.count("GET", r"^/public/download/") == 1, "stored after the first fetch"
    events = (await client.get(f"/api/v1/calls/{call['id']}/events", headers=owner.headers)).json()
    assert events[-1]["type"] == "transcript_cached"
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    assert (await client.get(url, headers=viewer.headers)).status_code == 200


async def test_calls_are_tenant_isolated(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    _simulate(monkeypatch)
    call = await start_call(client, owner, lead["id"], agent["id"])
    await complete_simulated_call({}, call["id"])

    other = await signup(client, "b@example.com", "Tenant B")
    url = f"/api/v1/calls/{call['id']}"
    for path in ("", "/events", "/transcript", "/recording"):
        assert (await client.get(url + path, headers=other.headers)).status_code == 404, path
    assert (await client.get("/api/v1/calls", headers=other.headers)).json()["total"] == 0
    assert (
        await client.get(f"/api/v1/leads/{lead['id']}/calls", headers=other.headers)
    ).status_code == 404
    r = await client.post(
        "/api/v1/calls",
        headers=other.headers,
        json={"lead_id": lead["id"], "agent_id": agent["id"]},
    )
    assert r.status_code == 404
    assert (await client.get("/api/v1/calls", headers=owner.headers)).json()["total"] == 1


@pytest.mark.usefixtures("live_calls")
async def test_reconciliation_recovers_lost_results_and_closes_stale_calls(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await published_agent(client, owner)
    leads = [await create_lead(client, owner, phone=f"+1415555020{i}") for i in range(4)]
    stale = await start_call(client, owner, leads[0]["id"], agent["id"], live=True)
    stale_run = max(runtime.runs)
    lost = await start_call(client, owner, leads[1]["id"], agent["id"], live=True)
    lost_run = max(runtime.runs)
    fresh = await start_call(client, owner, leads[2]["id"], agent["id"], live=True)
    _simulate(monkeypatch)
    stuck = await start_call(client, owner, leads[3]["id"], agent["id"])
    runtime.finish(lost_run, disposition="qualified")  # its webhook never arrived

    now = datetime.now(UTC)
    async with get_sessionmaker()() as db:
        for call_id, age in ((stale["id"], timedelta(hours=3)), (lost["id"], timedelta(minutes=3))):
            await db.execute(
                update(Call).where(Call.id == uuid.UUID(call_id)).values(started_at=now - age)
            )
        await db.execute(
            update(Call)
            .where(Call.id == uuid.UUID(stuck["id"]))
            .values(created_at=now - timedelta(minutes=5))
        )
        await db.commit()

    counts = await reconcile_calls({})
    assert counts["checked"] == 2 and counts["updated"] == 2 and counts["simulated_completed"] == 1
    assert counts["errors"] == 0 and counts["unreachable"] == 0

    async def state(call_id: str) -> dict[str, Any]:
        return (await client.get(f"/api/v1/calls/{call_id}", headers=owner.headers)).json()  # type: ignore[no-any-return]

    closed = await state(stale["id"])
    assert closed["state"] == "failed" and closed["error_code"] == "no_result"
    assert closed["ended_at"] is not None and "voice service" in closed["error_message"]
    recovered = await state(lost["id"])
    assert recovered["state"] == "completed" and recovered["outcome"] == "qualified"
    assert (await state(fresh["id"]))["state"] == "dialing", "not stale yet"
    assert (await state(stuck["id"]))["state"] == "completed"
    assert runtime.runs[stale_run]["is_completed"] is False
    timeline = (
        await client.get(f"/api/v1/leads/{leads[0]['id']}/activities", headers=owner.headers)
    ).json()
    assert any(a["summary"] == "Call failed" for a in timeline["items"])
    events = (await client.get(f"/api/v1/calls/{lost['id']}/events", headers=owner.headers)).json()
    assert events[-1]["data"]["source"] == "reconciliation"

    # Running again changes nothing (idempotent), and a late real result still wins over no_result.
    assert (await reconcile_calls({}))["updated"] == 0
    runtime.finish(stale_run, disposition="not_interested")
    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(stale["id"]))
        assert row is not None
        await call_service.sync_from_runtime(db, row)
        await db.commit()
    late = await state(stale["id"])
    assert (
        late["state"] == "completed"
        and late["outcome"] == "not_interested"
        and late["error_code"] is None
    )

    # Runtime unreachable: nothing is closed early.
    extra = await start_call(client, owner, leads[2]["id"], agent["id"], live=True)
    async with get_sessionmaker()() as db:
        await db.execute(
            update(Call)
            .where(Call.id == uuid.UUID(extra["id"]))
            .values(started_at=now - timedelta(minutes=5))
        )
        await db.commit()
    runtime.down = True
    counts = await reconcile_calls({})
    assert counts["unreachable"] == 1 and counts["updated"] == 0
    assert (await state(extra["id"]))["state"] == "dialing"


async def test_schedule_helper_survives_queue_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import jobs

    async def _unavailable(*_: Any, **__: Any) -> bool:
        raise jobs.QueueUnavailableError("down")

    monkeypatch.setattr(jobs, "enqueue", _unavailable)
    assert await call_service.schedule_simulated_completion(uuid.uuid4()) is False
    assert CallState.FAILED in call_service.TERMINAL_STATES
