"""Browser test calls: recorded under Calls, completed like live calls, never counted.

A test call starts from the agent editor (`POST /agents/{id}/test-session`). It must
appear under Calls with its transcript and response times once the conversation ends,
and must leave no trace anywhere else: lead history, consent, outbound webhooks,
analytics.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select, update

from app.core.redis import get_redis
from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.webhooks import DELIVERY_ID_HEADER
from app.integrations.voice_runtime.workflow_builder import WEBHOOK_SECRET_HEADER
from app.models.audit import AuditLog
from app.models.calls import Call, CallMode, CallState
from app.models.crm import Activity, ActivityType, ConsentStatus, Lead
from app.models.developer import OutboxEvent
from app.services import calls as call_service
from app.services import jobs
from app.workers.calls import reconcile_calls
from tests.calls_support import (
    CallsRuntime,
    create_lead,
    published_agent,
    runtime_org,
    start_call,
    webhook_secret,
    workflow_id,
)
from tests.conftest import Account, invite_and_join, signup

WEBHOOK = "/api/v1/webhooks/voice-runtime"
T0 = datetime(2026, 10, 19, 10, 0, 0, tzinfo=UTC)


class BrowserRuntime(CallsRuntime):
    """CallsRuntime whose embed sessions create runs, as the real runtime does.

    Verified: `POST /public/embed/init` creates the run at once with call_type inbound
    and the context variables plus `direction="inbound"` (routes/public_embed.py).
    """

    def __init__(self) -> None:
        super().__init__()
        self.embed_inits: list[dict[str, Any]] = []

    def _public(self, m: str, path: str, body: Any, request: httpx.Request) -> httpx.Response:
        response = super()._public(m, path, body, request)
        if m != "POST" or path != "/public/embed/init" or response.status_code != 200:
            return response
        org_id, wf_id, _ = self.embed_tokens[body["token"]]
        context = dict(body.get("context_variables") or {})
        self.embed_inits.append(context)
        rid = self.add_run(
            org_id,
            wf_id,
            initial_context={**context, "provider": "smallwebrtc", "direction": "inbound"},
            call_type="inbound",
        )
        self.runs[rid]["mode"] = "smallwebrtc"
        return httpx.Response(200, json={**json.loads(response.content), "workflow_run_id": rid})


@pytest.fixture
def runtime() -> Iterator[BrowserRuntime]:
    fake = BrowserRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        router.route(host="storage.test").mock(side_effect=fake.storage)
        yield fake


@pytest.fixture
def queue_down(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _unavailable(*_: Any, **__: Any) -> bool:
        raise jobs.QueueUnavailableError("redis down")

    monkeypatch.setattr(jobs, "enqueue", _unavailable)


def _events() -> list[dict[str, Any]]:
    """Greeting plus two answered turns (0.62 s measured, then 0.9 s from timestamps)."""

    def at(ms: int) -> str:
        return (T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds")

    def ev(type_: str, ts: int, payload: dict[str, Any], turn: int) -> dict[str, Any]:
        return {
            "type": type_,
            "timestamp": at(ts),
            "node_id": "n1",
            "payload": payload,
            "turn": turn,
        }

    def said(kind: str, turn: int, start: int, end: int, text: str) -> dict[str, Any]:
        payload = {"text": text, "final": True, "timestamp": at(start), "end_timestamp": at(end)}
        if kind == "bot":
            return ev("rtf-bot-text", start, payload, turn)
        return ev("rtf-user-transcription", end, payload, turn)

    return [
        said("bot", 0, 500, 3_000, "Hi, this is Sarah."),
        said("user", 1, 3_500, 4_400, "Hi."),
        ev("rtf-latency-measured", 5_020, {"latency_seconds": 0.62}, 1),
        said("bot", 1, 5_020, 8_000, "Is now a good time?"),
        said("user", 2, 8_500, 10_000, "Yes, go ahead."),
        said("bot", 2, 10_900, 13_000, "Great."),
    ]


async def _start(
    client: httpx.AsyncClient, acct: Account, agent_id: str, lead_id: str | None = None
) -> dict[str, Any]:
    r = await client.post(
        f"/api/v1/agents/{agent_id}/test-session", headers=acct.headers, json={"lead_id": lead_id}
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def _call(client: httpx.AsyncClient, acct: Account, call_id: str) -> dict[str, Any]:
    r = await client.get(f"/api/v1/calls/{call_id}", headers=acct.headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def _row(call_id: str) -> Call:
    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(call_id))
        assert row is not None
        return row


def _finish(runtime: BrowserRuntime, run_id: int, **kwargs: Any) -> None:
    runtime.finish(
        run_id,
        disposition="user_hangup",
        call_status=None,
        transcript="Agent: Hi, this is Sarah.\nUser: Hi.\nAgent: Is now a good time?",
        recording=b"RIFF....WAVE",
        **kwargs,
    )
    runtime.runs[run_id]["logs"] = {"realtime_feedback_events": _events()}


async def _outbox_call_events() -> list[OutboxEvent]:
    async with get_sessionmaker()() as db:
        rows = await db.execute(select(OutboxEvent).where(OutboxEvent.event_type.like("call.%")))
        return list(rows.scalars().all())


# ------------------------------------------------------------------ starting


async def test_test_session_records_a_test_call(
    client: httpx.AsyncClient, owner: Account, runtime: BrowserRuntime
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    session = await _start(client, owner, agent["id"], lead["id"])
    call_id = session["call_id"]

    call = await _call(client, owner, call_id)
    assert call["mode"] == "test" and call["direction"] == "inbound"
    assert call["state"] == "in_progress" and call["started_at"] is not None
    assert call["agent_id"] == agent["id"]
    assert call["agent_version"] == agent["published_version"]
    assert call["lead_id"] == lead["id"] and call["lead_name"] == "Hina"
    assert call["initiated_by_user_id"] == owner.user_id
    assert call["to_phone_e164"] is None and call["campaign_id"] is None

    # The session carries the call id, so the completion webhook finds the record.
    sent = runtime.embed_inits[-1]
    assert sent["atlas_call_id"] == call_id and sent["atlas_test"] == "true"
    assert sent["first_name"] == "Hina"
    row = await _row(call_id)
    run_id = max(runtime.runs)
    assert row.runtime_run_id == str(run_id)
    assert row.runtime_workflow_id == str(workflow_id(runtime_org(runtime)))

    events = (await client.get(f"/api/v1/calls/{call_id}/events", headers=owner.headers)).json()
    assert [e["type"] for e in events] == ["created", "state_changed"]
    assert events[0]["data"] == {"direction": "inbound", "mode": "test", "lead_id": lead["id"]}
    assert events[1]["data"] == {"from": "queued", "to": "in_progress", "reason": "test_session"}

    async with get_sessionmaker()() as db:
        audit = (
            await db.execute(select(AuditLog).where(AuditLog.action == "agent.test_call_started"))
        ).scalar_one()
    assert audit.changes == {"lead_id": lead["id"], "call_id": call_id}
    assert await _outbox_call_events() == [], "test calls are never sent to webhooks"

    listed = (await client.get("/api/v1/calls", headers=owner.headers)).json()
    assert listed["total"] == 1 and listed["items"][0]["mode"] == "test"
    only_test = await client.get("/api/v1/calls", headers=owner.headers, params={"mode": "test"})
    assert only_test.json()["total"] == 1
    only_live = await client.get("/api/v1/calls", headers=owner.headers, params={"mode": "live"})
    assert only_live.json()["total"] == 0

    # Without a lead: sample variables, no lead on the record.
    bare = await _start(client, owner, agent["id"])
    assert (await _call(client, owner, bare["call_id"]))["lead_id"] is None
    assert runtime.embed_inits[-1]["atlas_call_id"] == bare["call_id"]


async def test_a_session_that_cannot_start_leaves_no_call(
    client: httpx.AsyncClient, owner: Account, runtime: BrowserRuntime
) -> None:
    agent = await published_agent(client, owner)
    runtime.down = True
    r = await client.post(
        f"/api/v1/agents/{agent['id']}/test-session", headers=owner.headers, json={}
    )
    assert r.status_code >= 500
    assert (await client.get("/api/v1/calls", headers=owner.headers)).json()["total"] == 0


# ------------------------------------------------------------------ completion


@pytest.mark.usefixtures("queue_down")
async def test_completion_webhook_fills_transcript_recording_and_latency(
    client: httpx.AsyncClient, owner: Account, runtime: BrowserRuntime
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call_id = (await _start(client, owner, agent["id"], lead["id"]))["call_id"]
    run_id = max(runtime.runs)
    # The tester says "don't call me again" during the test: the lead must not change.
    _finish(runtime, run_id, extracted={"opted_out": True, "budget": "500k"})

    org = runtime_org(runtime)
    payload = {
        "atlas_call_id": call_id,
        "workflow_run_id": str(run_id),
        "workflow_id": str(workflow_id(org)),
        "direction": "inbound",
    }
    headers = {DELIVERY_ID_HEADER: str(uuid.uuid4()), WEBHOOK_SECRET_HEADER: webhook_secret(org)}
    r = await client.post(WEBHOOK, headers=headers, json=payload)
    assert r.status_code == 202 and r.json()["status"] == "processed", r.text

    call = await _call(client, owner, call_id)
    assert call["state"] == "completed" and call["duration_seconds"] == 42
    assert call["outcome"] == "user_hangup" and call["captured"] == {"budget": "500k"}
    assert call["transcript_available"] is True and call["recording_available"] is True
    assert call["ended_at"] is not None
    summary = call["latency_summary"]
    assert summary["source"] == "runtime" and summary["measured_turns"] == 2
    assert summary["response_p50_ms"] == 760  # 620 measured, 900 from timestamps

    latency = (await client.get(f"/api/v1/calls/{call_id}/latency", headers=owner.headers)).json()
    assert [t["response_ms"] for t in latency["turns"] if t["response_ms"] is not None] == [
        620,
        900,
    ]
    transcript = (
        await client.get(f"/api/v1/calls/{call_id}/transcript", headers=owner.headers)
    ).json()
    assert transcript["mode"] == "test" and transcript["simulated"] is False
    assert transcript["text"].startswith("Agent: Hi, this is Sarah.")
    row = await _row(call_id)
    assert row.runtime_transcript_key == f"transcripts/{run_id}.txt"
    assert row.runtime_recording_key == f"recordings/{run_id}.wav"

    # No trace outside the record: lead, timeline, consent, outbound webhooks.
    async with get_sessionmaker()() as db:
        stored = await db.get(Lead, uuid.UUID(lead["id"]))
        assert stored is not None
        assert stored.do_not_call is False and stored.consent_status != ConsentStatus.REVOKED
        assert stored.last_contacted_at is None
        calls_on_timeline = (
            await db.execute(select(Activity).where(Activity.type == ActivityType.CALL))
        ).scalars()
        assert list(calls_on_timeline) == []
    assert call["opted_out"] is True, "the record says what happened; the lead is left alone"
    assert await _outbox_call_events() == []
    lead_calls = (
        await client.get(f"/api/v1/leads/{lead['id']}/calls", headers=owner.headers)
    ).json()
    assert lead_calls["total"] == 0, "the lead was never contacted"
    by_lead = await client.get(
        "/api/v1/calls", headers=owner.headers, params={"lead_id": lead["id"]}
    )
    assert by_lead.json()["total"] == 1, "still findable from Calls"

    # A repeated delivery changes nothing.
    headers[DELIVERY_ID_HEADER] = str(uuid.uuid4())
    r = await client.post(WEBHOOK, headers=headers, json=payload)
    assert r.status_code == 202
    assert (await _call(client, owner, call_id))["latency_summary"] == summary


async def test_reconciliation_completes_and_closes_test_calls(
    client: httpx.AsyncClient, owner: Account, runtime: BrowserRuntime
) -> None:
    agent = await published_agent(client, owner)
    lost = (await _start(client, owner, agent["id"]))["call_id"]
    lost_run = max(runtime.runs)
    abandoned = (await _start(client, owner, agent["id"]))["call_id"]
    recent = (await _start(client, owner, agent["id"]))["call_id"]
    _finish(runtime, lost_run)  # its webhook never arrived

    now = datetime.now(UTC)
    async with get_sessionmaker()() as db:
        for call_id, age in (
            (lost, timedelta(minutes=3)),
            (abandoned, call_service.TEST_GIVE_UP_AFTER + timedelta(minutes=1)),
            (recent, timedelta(minutes=30)),
        ):
            await db.execute(
                update(Call).where(Call.id == uuid.UUID(call_id)).values(started_at=now - age)
            )
        await db.commit()

    counts = await reconcile_calls({})
    assert counts["checked"] == 3 and counts["updated"] == 2 and counts["errors"] == 0

    done = await _call(client, owner, lost)
    assert done["state"] == "completed" and done["latency_summary"]["source"] == "runtime"
    closed = await _call(client, owner, abandoned)
    assert closed["state"] == "canceled" and closed["error_code"] == "not_completed"
    assert (await _call(client, owner, recent))["state"] == "in_progress", "may still be live"
    assert await _outbox_call_events() == []
    assert (await reconcile_calls({}))["updated"] == 0, "idempotent"


# ------------------------------------------------------------------ the browser reports the end


async def test_end_report_closes_unconnected_calls_and_schedules_a_sync(
    client: httpx.AsyncClient, owner: Account, runtime: BrowserRuntime
) -> None:
    agent = await published_agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}/test-calls"

    # Microphone blocked: nothing will ever come back, so it closes at once.
    never = (await _start(client, owner, agent["id"]))["call_id"]
    never_run = max(runtime.runs)
    r = await client.post(f"{url}/{never}/end", headers=owner.headers, json={"connected": False})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "canceled" and body["error_code"] == "not_connected"
    again = await client.post(f"{url}/{never}/end", headers=owner.headers, json={"connected": True})
    assert again.json()["state"] == "canceled", "a finished call is left alone"
    # Should the session have connected after all, its real result still wins.
    _finish(runtime, never_run)
    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(never))
        assert row is not None
        await call_service.sync_from_runtime(db, row)
        await db.commit()
    healed = await _call(client, owner, never)
    assert healed["state"] == "completed" and healed["error_code"] is None

    # Connected: the run is fetched again shortly (covers a missing webhook).
    talked = (await _start(client, owner, agent["id"]))["call_id"]
    r = await client.post(f"{url}/{talked}/end", headers=owner.headers, json={"connected": True})
    assert r.status_code == 200 and r.json()["state"] == "in_progress"
    assert await get_redis().exists(f"arq:job:test-call-ended:{talked}") == 1

    # Only test calls of this agent, in this organization.
    other_agent = str(uuid.uuid4())
    r = await client.post(
        f"/api/v1/agents/{other_agent}/test-calls/{talked}/end",
        headers=owner.headers,
        json={"connected": False},
    )
    assert r.status_code == 404
    lead = await create_lead(client, owner)
    simulated = await start_call(client, owner, lead["id"], agent["id"])
    r = await client.post(
        f"{url}/{simulated['id']}/end", headers=owner.headers, json={"connected": False}
    )
    assert r.status_code == 404
    assert (await _call(client, owner, simulated["id"]))["state"] == "queued"


# ------------------------------------------------------------------ never counted


async def test_analytics_and_campaign_figures_exclude_test_calls(
    client: httpx.AsyncClient, owner: Account, runtime: BrowserRuntime
) -> None:
    agent = await published_agent(client, owner)
    call_id = (await _start(client, owner, agent["id"]))["call_id"]
    _finish(runtime, max(runtime.runs))
    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(call_id))
        assert row is not None
        await call_service.sync_from_runtime(db, row)
        await db.commit()
    assert (await _call(client, owner, call_id))["latency_summary"]["source"] == "runtime"

    overview_url = "/api/v1/analytics/calls-overview"
    o = (await client.get(overview_url, headers=owner.headers)).json()
    assert o["totals"]["calls"] == 0 and o["totals"]["answered"] == 0
    assert o["answer_rate"] is None and o["total_duration_seconds"] == 0
    assert o["by_agent"] == [] and o["outcomes"] == []
    assert o["measured_calls"] == 0 and o["response_p50_ms"] is None
    assert sum(d["answered"] for d in o["daily"]) == 0
    assert o["mode_split"] == {"live": 0, "simulated": 0, "test": 1}

    agent_view = await client.get(
        overview_url, headers=owner.headers, params={"agent_id": agent["id"]}
    )
    assert agent_view.json()["totals"]["calls"] == 0
    test_view = (
        await client.get(overview_url, headers=owner.headers, params={"mode": "test"})
    ).json()
    assert test_view["totals"]["calls"] == 0 and test_view["mode_split"]["test"] == 1
    live_view = (
        await client.get(overview_url, headers=owner.headers, params={"mode": "live"})
    ).json()
    assert live_view["mode_split"]["test"] == 0

    async with get_sessionmaker()() as db:
        row = await db.get(Call, uuid.UUID(call_id))
        assert row is not None and row.mode == CallMode.TEST
        assert row.campaign_id is None, "campaign statistics count campaign calls only"


# ------------------------------------------------------------------ tenancy and roles


async def test_test_calls_are_tenant_isolated_and_role_gated(
    client: httpx.AsyncClient, owner: Account, runtime: BrowserRuntime
) -> None:
    agent = await published_agent(client, owner)
    call_id = (await _start(client, owner, agent["id"]))["call_id"]
    end_url = f"/api/v1/agents/{agent['id']}/test-calls/{call_id}/end"

    other = await signup(client, "other@example.com", org="Other Co")
    assert (await client.get(f"/api/v1/calls/{call_id}", headers=other.headers)).status_code == 404
    assert (await client.get("/api/v1/calls", headers=other.headers)).json()["total"] == 0
    r = await client.post(end_url, headers=other.headers, json={"connected": False})
    assert r.status_code == 404
    o = (await client.get("/api/v1/analytics/calls-overview", headers=other.headers)).json()
    assert o["mode_split"]["test"] == 0

    # Another tenant's webhook secret cannot complete this tenant's test call.
    run_id = max(runtime.runs)
    _finish(runtime, run_id)
    r = await client.post(
        WEBHOOK,
        headers={DELIVERY_ID_HEADER: str(uuid.uuid4()), WEBHOOK_SECRET_HEADER: "not-the-secret"},
        json={"atlas_call_id": call_id, "workflow_run_id": str(run_id)},
    )
    assert r.status_code == 401

    # Reading needs calls:read (every role); ending needs the same right as starting.
    viewer = await invite_and_join(client, owner, "rep@example.com", "agent")
    assert (await client.get(f"/api/v1/calls/{call_id}", headers=viewer.headers)).status_code == 200
    r = await client.post(end_url, headers=viewer.headers, json={"connected": False})
    assert r.status_code == 403
    assert (await _row(call_id)).state == CallState.IN_PROGRESS
