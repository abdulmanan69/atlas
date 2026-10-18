"""Campaigns: state machine, audience preview, safety gates, scheduler, retries, isolation.

The scheduler is exercised by calling its job functions directly with a fake ARQ
context. The fake voice runtime is only needed for the live path.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx
from sqlalchemy import select

from app.core.config import get_settings
from app.core.exceptions import InvalidStateTransitionError
from app.db.session import get_sessionmaker
from app.models.audit import AuditLog
from app.models.calls import Call, CallMode, CallState
from app.models.campaigns import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    WEEKDAYS,
    Campaign,
    CampaignStatus,
    transition,
)
from app.services import calls as call_service
from app.services import campaigns as campaign_service
from app.services import events
from app.workers import campaigns as worker
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY, FakeRuntime

ALL_DAY = {d: [["00:00", "24:00"]] for d in WEEKDAYS}
_phones = iter(range(1000, 9999))
# Frozen clocks live in the future so real database timestamps never fall inside the
# dispatcher's "calls started in the last minute" window.
SUNDAY_NOON_UTC = datetime(2027, 2, 28, 12, 0, tzinfo=UTC)
MONDAY_0500_UTC = datetime(2027, 3, 1, 5, 0, tzinfo=UTC)  # 10:00 in Asia/Karachi (UTC+5)


class CallRuntime(FakeRuntime):
    """The fake voice runtime plus the outbound-call trigger; `capacity_full` answers 429."""

    def __init__(self) -> None:
        super().__init__()
        self.capacity_full = False
        self.triggered: list[dict[str, Any]] = []

    def _workflows(self, org: Any, m: str, path: str, body: Any) -> httpx.Response:
        if m == "POST" and re.fullmatch(r"/public/agent/workflow/[\w-]+", path):
            if self.capacity_full:
                return httpx.Response(429, json={"detail": "Concurrent call limit reached"})
            self.triggered.append(body)
            return httpx.Response(
                200, json={"status": "queued", "workflow_run_id": next(self._ids)}
            )
        return super()._workflows(org, m, path, body)


@pytest.fixture
def runtime() -> Iterator[CallRuntime]:
    fake = CallRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        yield fake


@pytest.fixture
def ctx(scheduled: list[tuple[uuid.UUID, int]]) -> dict[str, Any]:
    """The ARQ job context; the campaign jobs keep nothing in it."""
    return {}


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[tuple[uuid.UUID, int]]:
    """Records simulated-call completions the dispatcher asks the call core to schedule."""
    seen: list[tuple[uuid.UUID, int]] = []

    async def _schedule(call_id: uuid.UUID, delay_seconds: int = 0) -> bool:
        seen.append((call_id, delay_seconds))
        return True

    monkeypatch.setattr(call_service, "schedule_simulated_completion", _schedule)
    return seen


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Campaign events handed to the integrations layer (other domains' events are ignored)."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def _emit(db: Any, org_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> None:
        if event_type.startswith("campaign."):
            seen.append((event_type, data))

    monkeypatch.setattr(events, "emit", _emit)
    return seen


def freeze(monkeypatch: pytest.MonkeyPatch, at: datetime) -> None:
    monkeypatch.setattr(campaign_service, "utcnow", lambda: at)


# ------------------------------------------------------------------ helpers


async def _published_agent(client: httpx.AsyncClient, acct: Account) -> str:
    ids = {}
    for provider in ("openai", "deepgram", "elevenlabs"):
        r = await client.post(
            "/api/v1/ai/connections",
            headers=acct.headers,
            json={"name": provider, "provider": provider, "credentials": {"api_key": VALID_KEY}},
        )
        assert r.status_code == 201, r.text
        ids[provider] = r.json()["id"]
    r = await client.put(
        "/api/v1/ai/models",
        headers=acct.headers,
        json={
            "llm": {"connection_id": ids["openai"], "settings": {"model": "gpt-4.1-mini"}},
            "stt": {"connection_id": ids["deepgram"], "settings": {}},
            "tts": {"connection_id": ids["elevenlabs"], "settings": {"voice": "v1"}},
        },
    )
    assert r.status_code == 200, r.text
    r = await client.post("/api/v1/agents", headers=acct.headers, json={"name": "Qualifier"})
    assert r.status_code == 201, r.text
    agent_id = str(r.json()["id"])
    r = await client.post(f"/api/v1/agents/{agent_id}/publish", headers=acct.headers, json={})
    assert r.status_code == 200, r.text
    return agent_id


async def _leads(
    client: httpx.AsyncClient, acct: Account, n: int, *, tag: str = "camp", **extra: object
) -> list[str]:
    ids = []
    for i in range(n):
        r = await client.post(
            "/api/v1/leads",
            headers=acct.headers,
            json={
                "first_name": f"Lead{i}",
                "phone": f"+44 20 7946 {next(_phones)}",
                "tags": [tag],
                **extra,
            },
        )
        assert r.status_code == 201, r.text
        ids.append(str(r.json()["id"]))
    return ids


async def _campaign(
    client: httpx.AsyncClient, acct: Account, agent_id: str, **body: object
) -> dict[str, Any]:
    r = await client.post(
        "/api/v1/campaigns",
        headers=acct.headers,
        json={
            "name": "Test campaign",
            "agent_id": agent_id,
            "lead_filter": {"tag": "camp"},
            "calling_hours": ALL_DAY,
            "max_concurrency": 5,
            **body,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()  # type: ignore[no-any-return]


async def _started(
    client: httpx.AsyncClient, acct: Account, agent_id: str, **body: object
) -> dict[str, Any]:
    campaign = await _campaign(client, acct, agent_id, **body)
    r = await client.post(f"/api/v1/campaigns/{campaign['id']}/start", headers=acct.headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def _calls(campaign_id: str) -> list[Call]:
    async with get_sessionmaker()() as db:
        rows = await db.execute(
            select(Call).where(Call.campaign_id == uuid.UUID(campaign_id)).order_by(Call.created_at)
        )
        return list(rows.scalars().all())


async def _finish_call(call_id: uuid.UUID, state: CallState, outcome: str | None = None) -> None:
    """Shortcut for a runtime result: the reconciler only looks at the call's state."""
    async with get_sessionmaker()() as db, db.begin():
        call = await db.get(Call, call_id)
        assert call is not None
        call.state = state
        call.outcome = outcome
        call.ended_at = datetime.now(UTC)
        call.duration_seconds = 120 if state == CallState.COMPLETED else 0


async def _campaign_row(campaign_id: str) -> Campaign:
    async with get_sessionmaker()() as db:
        row = await db.get(Campaign, uuid.UUID(campaign_id))
        assert row is not None
        return row


async def _items(
    client: httpx.AsyncClient, acct: Account, campaign_id: str
) -> list[dict[str, Any]]:
    r = await client.get(f"/api/v1/campaigns/{campaign_id}/leads", headers=acct.headers)
    assert r.status_code == 200, r.text
    return list(r.json()["items"])


async def _get(client: httpx.AsyncClient, acct: Account, campaign_id: str) -> dict[str, Any]:
    r = await client.get(f"/api/v1/campaigns/{campaign_id}", headers=acct.headers)
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


async def _run_simulated(scheduled: list[tuple[uuid.UUID, int]]) -> int:
    """Do what the worker's completion job does once the delay has passed."""
    pending, scheduled[:] = list(scheduled), []
    for call_id, delay in pending:
        assert 5 <= delay <= 20
        async with get_sessionmaker()() as db, db.begin():
            await call_service.complete_simulated_call(db, call_id)
    return len(pending)


# ------------------------------------------------------------------ state machine


@pytest.mark.parametrize(
    ("source", "target"), [(s, t) for s in CampaignStatus for t in CampaignStatus]
)
def test_state_machine_table(source: CampaignStatus, target: CampaignStatus) -> None:
    campaign = Campaign(status=source)
    if target in TRANSITIONS[source]:
        transition(campaign, target)
        assert campaign.status == target
    else:
        with pytest.raises(InvalidStateTransitionError) as exc:
            transition(campaign, target)
        assert campaign.status == source
        assert exc.value.status_code == 409


def test_state_machine_shape() -> None:
    assert all(not TRANSITIONS[s] for s in TERMINAL_STATUSES)
    assert CampaignStatus.COMPLETED not in TRANSITIONS[CampaignStatus.DRAFT]
    assert CampaignStatus.RUNNING in TRANSITIONS[CampaignStatus.PAUSED]
    assert CampaignStatus.DRAFT not in {t for ts in TRANSITIONS.values() for t in ts}


# ------------------------------------------------------------------ audience


async def test_preview_counts_skips_by_reason_without_side_effects(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 3)
    await _leads(client, owner, 1, tag="camp", do_not_call=True)
    await client.post(
        "/api/v1/leads",
        headers=owner.headers,
        json={"first_name": "Revoked", "phone": "+442079469001", "tags": ["camp"]},
    )
    revoked = (
        await client.get("/api/v1/leads", headers=owner.headers, params={"q": "revoked"})
    ).json()["items"][0]
    await client.patch(
        f"/api/v1/leads/{revoked['id']}",
        headers=owner.headers,
        json={"consent_status": "revoked"},
    )
    await client.post(
        "/api/v1/leads",
        headers=owner.headers,
        json={"first_name": "NoPhone", "email": "np@example.com", "tags": ["camp"]},
    )
    await _leads(client, owner, 2, tag="other")

    campaign = await _campaign(client, owner, agent, max_calls=2)
    r = await client.post(f"/api/v1/campaigns/{campaign['id']}/preview", headers=owner.headers)
    assert r.status_code == 200, r.text
    p = r.json()
    assert (p["matched"], p["eligible"]) == (6, 3)
    # Revoking consent also sets do-not-call, so it is reported under the stricter reason.
    assert p["skipped"] == {"do_not_call": 2, "no_phone": 1}
    assert p["capped_by_max_calls"] is True
    assert p["estimated_duration_seconds"] > 0 and p["calling_window_open"] is True
    assert await _items(client, owner, campaign["id"]) == []
    assert (await _get(client, owner, campaign["id"]))["status"] == "draft"


async def test_create_validation(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime
) -> None:
    agent = await _published_agent(client, owner)
    base = {"name": "x", "agent_id": agent, "lead_filter": {"tag": "camp"}}

    async def post(**body: object) -> int:
        r = await client.post("/api/v1/campaigns", headers=owner.headers, json={**base, **body})
        return r.status_code

    assert await post(timezone="Mars/Olympus") == 422
    assert await post(lead_filter=None) == 422
    assert await post(lead_filter={}) == 422
    assert await post(calling_hours={"mon": [["18:00", "09:00"]]}) == 422
    assert await post(calling_hours={"mon": [["9am", "5pm"]]}) == 422
    assert await post(max_concurrency=51) == 422
    assert await post(start_at="2030-01-02T00:00:00Z", end_at="2030-01-01T00:00:00Z") == 422
    assert await post(agent_id=str(uuid.uuid4())) == 422
    assert await post(lead_filter=None, lead_ids=[str(uuid.uuid4())]) == 201

    draft_agent = (
        await client.post("/api/v1/agents", headers=owner.headers, json={"name": "Draft"})
    ).json()["id"]
    await _leads(client, owner, 1)
    campaign = await _campaign(client, owner, draft_agent)
    r = await client.post(f"/api/v1/campaigns/{campaign['id']}/start", headers=owner.headers)
    assert r.status_code == 422 and "Publish the agent" in r.json()["error"]["message"]

    empty = await _campaign(client, owner, agent, lead_filter={"tag": "nobody"})
    r = await client.post(f"/api/v1/campaigns/{empty['id']}/start", headers=owner.headers)
    assert r.status_code == 422 and r.json()["error"]["details"]["eligible"] == 0


# ------------------------------------------------------------------ dry run end to end


async def test_dry_run_places_only_simulated_calls_and_completes(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallRuntime,
    ctx: dict[str, Any],
    scheduled: list[tuple[uuid.UUID, int]],
    emitted: list[tuple[str, dict[str, Any]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Even on a deployment that allows live calls, a dry run never dials.
    monkeypatch.setattr(get_settings(), "live_calls_enabled", True)
    agent = await _published_agent(client, owner)
    lead_ids = await _leads(client, owner, 3)
    campaign = await _started(
        client, owner, agent, retry_policy={"max_attempts": 1, "retry_on": ["no_answer"]}
    )
    assert campaign["status"] == "running" and campaign["started_at"]
    assert campaign["started_by_user_id"] == owner.user_id
    assert [e for e, _ in emitted] == ["campaign.started"]
    assert all(i["status"] == "pending" for i in await _items(client, owner, campaign["id"]))

    result = await worker.dispatch_campaigns(ctx)
    assert result["dispatched"] == 3
    calls = await _calls(campaign["id"])
    assert len(calls) == 3
    assert {c.mode for c in calls} == {CallMode.SIMULATED}
    assert {c.state for c in calls} == {CallState.QUEUED}
    assert runtime.triggered == [], "no call reached the voice service"
    assert {str(c.lead_id) for c in calls} == set(lead_ids)
    items = await _items(client, owner, campaign["id"])
    assert all(i["status"] == "calling" and i["attempts"] == 1 for i in items)

    assert await _run_simulated(scheduled) == 3
    assert await _run_simulated(scheduled) == 0
    # Completing a call twice is harmless.
    async with get_sessionmaker()() as db, db.begin():
        await call_service.complete_simulated_call(db, calls[0].id)
    calls = await _calls(campaign["id"])
    assert all(c.state in (CallState.COMPLETED, CallState.NO_ANSWER) for c in calls)

    advanced = await worker.advance_campaign_leads(ctx)
    assert advanced["reconciled"] == 3 and advanced["completed"] == 1
    done = await _get(client, owner, campaign["id"])
    assert done["status"] == "completed" and done["finished_at"]
    assert [e for e, _ in emitted] == ["campaign.started", "campaign.completed"]
    statuses = {i["status"] for i in await _items(client, owner, campaign["id"])}
    assert statuses <= {"completed", "exhausted"}

    progress = (
        await client.get(f"/api/v1/campaigns/{campaign['id']}/progress", headers=owner.headers)
    ).json()
    assert progress["calls_total"] == 3 and progress["status"] == "completed"
    assert sum(progress["leads"].values()) == 3
    assert progress["answer_rate"] is None or 0 <= progress["answer_rate"] <= 1
    assert done["stats"]["calls"]["total"] == 3

    answered = next((c for c in calls if c.state == CallState.COMPLETED), None)
    if answered is not None:
        timeline = (
            await client.get(f"/api/v1/leads/{answered.lead_id}/activities", headers=owner.headers)
        ).json()
        assert any(a["summary"].startswith("[Simulated] Call completed") for a in timeline["items"])


# ------------------------------------------------------------------ live gates


async def test_live_requires_permission_and_deployment_flag(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallRuntime,
    ctx: dict[str, Any],
    scheduled: list[tuple[uuid.UUID, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 2)
    manager = await invite_and_join(client, owner, "mgr@example.com", "manager")
    campaign = await _campaign(client, manager, agent, mode="live")
    url = f"/api/v1/campaigns/{campaign['id']}"

    # Manager may manage campaigns but cannot authorise real calls.
    r = await client.post(f"{url}/start", headers=manager.headers)
    assert r.status_code == 403 and r.json()["error"]["code"] == "forbidden"
    # Owner holds the permission, but the deployment forbids live calls.
    r = await client.post(f"{url}/start", headers=owner.headers)
    assert r.status_code == 403 and "ATLAS_LIVE_CALLS_ENABLED" in r.json()["error"]["message"]
    assert (await _get(client, owner, campaign["id"]))["status"] == "draft"
    assert await _items(client, owner, campaign["id"]) == []

    monkeypatch.setattr(get_settings(), "live_calls_enabled", True)
    r = await client.post(f"{url}/start", headers=owner.headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "running"
    async with get_sessionmaker()() as db:
        authorised = (
            await db.execute(select(AuditLog).where(AuditLog.action == "campaign.live_authorized"))
        ).scalar_one()
    assert str(authorised.actor_user_id) == owner.user_id
    assert authorised.resource_id == campaign["id"]

    result = await worker.dispatch_campaigns(ctx)
    assert result["dispatched"] == 2
    assert len(runtime.triggered) == 2
    calls = await _calls(campaign["id"])
    assert {c.mode for c in calls} == {CallMode.LIVE}
    assert {c.state for c in calls} == {CallState.DIALING}
    assert all(c.runtime_run_id for c in calls)
    assert scheduled == [], "live calls are completed by the runtime, not simulated"
    sent = runtime.triggered[0]["initial_context"]
    assert sent["atlas_campaign_id"] == campaign["id"] and sent["atlas_call_id"]

    # If the deployment flag is turned off while running, the worker parks the campaign.
    monkeypatch.setattr(get_settings(), "live_calls_enabled", False)
    await _finish_call(calls[0].id, CallState.COMPLETED, "qualified")
    await _finish_call(calls[1].id, CallState.COMPLETED, "not_interested")
    await worker.advance_campaign_leads(ctx)
    await _leads(client, owner, 1, tag="late")
    late = await _campaign(client, owner, agent, mode="live", lead_filter={"tag": "late"})
    monkeypatch.setattr(get_settings(), "live_calls_enabled", True)
    assert (
        await client.post(f"/api/v1/campaigns/{late['id']}/start", headers=owner.headers)
    ).status_code == 200
    monkeypatch.setattr(get_settings(), "live_calls_enabled", False)
    await worker.dispatch_campaigns(ctx)
    parked = await _get(client, owner, late["id"])
    assert parked["status"] == "paused" and "disabled" in parked["last_error"]
    assert len(runtime.triggered) == 2


async def test_runtime_capacity_backs_off_the_campaign(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallRuntime,
    ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "live_calls_enabled", True)
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 1)
    campaign = await _started(client, owner, agent, mode="live")
    runtime.capacity_full = True

    result = await worker.dispatch_campaigns(ctx)
    assert result.get("backoff") == 1 and result["dispatched"] == 0
    assert await _calls(campaign["id"]) == [], "the refused placement left no call record"
    row = await _campaign_row(campaign["id"])
    assert row.backoff_until is not None and row.backoff_until > datetime.now(UTC)
    items = await _items(client, owner, campaign["id"])
    assert items[0]["status"] == "pending" and items[0]["attempts"] == 0

    runtime.capacity_full = False
    assert (await worker.dispatch_campaigns(ctx))["campaigns"] == 0, "still backing off"
    freeze(monkeypatch, datetime.now(UTC) + timedelta(seconds=61))
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 1
    assert len(runtime.triggered) == 1


# ------------------------------------------------------------------ calling hours


async def test_calling_hours_follow_the_lead_timezone(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallRuntime,
    ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await _published_agent(client, owner)
    [karachi] = await _leads(client, owner, 1, timezone="Asia/Karachi")
    [no_zone] = await _leads(client, owner, 1, tag="camp2")
    await client.post(
        "/api/v1/leads/bulk",
        headers=owner.headers,
        json={"lead_ids": [no_zone], "action": "add_tags", "tags": ["camp"]},
    )
    freeze(monkeypatch, SUNDAY_NOON_UTC)
    campaign = await _started(
        client, owner, agent, timezone="UTC", calling_hours={"mon": [["09:00", "18:00"]]}
    )
    r = await client.post(f"/api/v1/campaigns/{campaign['id']}/preview", headers=owner.headers)
    assert r.json()["calling_window_open"] is False
    assert r.json()["next_window_opens_at"] == "2027-03-01T09:00:00Z"

    result = await worker.dispatch_campaigns(ctx)
    assert result["deferred"] == 2 and result["dispatched"] == 0
    assert await _calls(campaign["id"]) == []
    by_lead = {i["lead_id"]: i for i in await _items(client, owner, campaign["id"])}
    assert by_lead[karachi]["next_attempt_at"] == "2027-03-01T04:00:00Z"  # Monday 09:00 PKT
    assert by_lead[no_zone]["next_attempt_at"] == "2027-03-01T09:00:00Z"  # Monday 09:00 UTC

    # Monday 10:00 in Karachi but 05:00 UTC: only the Karachi lead is inside its window.
    freeze(monkeypatch, MONDAY_0500_UTC)
    result = await worker.dispatch_campaigns(ctx)
    assert result["dispatched"] == 1
    calls = await _calls(campaign["id"])
    assert [str(c.lead_id) for c in calls] == [karachi]
    assert await _items(client, owner, campaign["id"])


# ------------------------------------------------------------------ pacing


async def test_concurrency_cap_and_restart_idempotency(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime, ctx: dict[str, Any]
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 5)
    campaign = await _started(
        client, owner, agent, max_concurrency=2, retry_policy={"max_attempts": 1}
    )
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 2
    # A second worker (or a restart) finds the slots taken and places nothing new.
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 0
    calls = await _calls(campaign["id"])
    assert len(calls) == 2
    progress = (
        await client.get(f"/api/v1/campaigns/{campaign['id']}/progress", headers=owner.headers)
    ).json()
    assert progress["leads"] == {**progress["leads"], "pending": 3, "calling": 2}
    assert progress["eta_seconds"] > 0

    for call in calls:
        await _finish_call(call.id, CallState.COMPLETED, "qualified")
    first = await worker.advance_campaign_leads(ctx)
    second = await worker.advance_campaign_leads(ctx)
    assert first["reconciled"] == 2 and second["reconciled"] == 0
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 2
    assert len(await _calls(campaign["id"])) == 4
    progress = (
        await client.get(f"/api/v1/campaigns/{campaign['id']}/progress", headers=owner.headers)
    ).json()
    assert progress["calls_by_outcome"] == {"qualified": 2}
    assert progress["answer_rate"] == 1.0 and progress["average_duration_seconds"] == 120


async def test_calls_per_minute_budget(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime, ctx: dict[str, Any]
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 3)
    campaign = await _started(client, owner, agent, max_concurrency=5, calls_per_minute=1)
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 1
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 0
    assert len(await _calls(campaign["id"])) == 1


async def test_max_calls_usage_limit(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime, ctx: dict[str, Any]
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 3)
    campaign = await _started(client, owner, agent, max_calls=1)
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 1
    [call] = await _calls(campaign["id"])
    await _finish_call(call.id, CallState.COMPLETED, "qualified")
    await worker.advance_campaign_leads(ctx)
    assert (await _get(client, owner, campaign["id"]))["status"] == "running"
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 0
    assert len(await _calls(campaign["id"])) == 1
    reasons = [i["skip_reason"] for i in await _items(client, owner, campaign["id"])]
    assert reasons.count("usage_limit_reached") == 2
    await worker.advance_campaign_leads(ctx)
    assert (await _get(client, owner, campaign["id"]))["status"] == "completed"


# ------------------------------------------------------------------ retries


async def test_retry_after_no_answer_then_exhaustion(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallRuntime,
    ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await _published_agent(client, owner)
    [retried, failed] = await _leads(client, owner, 2)
    campaign = await _started(
        client,
        owner,
        agent,
        retry_policy={"max_attempts": 2, "retry_on": ["no_answer"], "backoff_minutes": 30},
    )
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 2
    calls = {str(c.lead_id): c for c in await _calls(campaign["id"])}
    await _finish_call(calls[retried].id, CallState.NO_ANSWER)
    await _finish_call(calls[failed].id, CallState.FAILED)
    await worker.advance_campaign_leads(ctx)

    by_lead = {i["lead_id"]: i for i in await _items(client, owner, campaign["id"])}
    assert by_lead[failed]["status"] == "failed" and by_lead[failed]["last_outcome"] == "failed"
    item = by_lead[retried]
    assert item["status"] == "pending" and item["attempts"] == 1
    assert item["last_outcome"] == "no_answer"
    due = datetime.fromisoformat(item["next_attempt_at"])
    assert timedelta(minutes=29) < due - datetime.now(UTC) <= timedelta(minutes=30)
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 0, "not due yet"
    assert (await _get(client, owner, campaign["id"]))["status"] == "running"

    freeze(monkeypatch, datetime.now(UTC) + timedelta(minutes=31))
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 1
    second = [c for c in await _calls(campaign["id"]) if str(c.lead_id) == retried][-1]
    await _finish_call(second.id, CallState.NO_ANSWER)
    await worker.advance_campaign_leads(ctx)
    by_lead = {i["lead_id"]: i for i in await _items(client, owner, campaign["id"])}
    assert by_lead[retried]["status"] == "exhausted" and by_lead[retried]["attempts"] == 2
    done = await _get(client, owner, campaign["id"])
    assert done["status"] == "completed" and done["stats"]["leads"] == {
        "exhausted": 1,
        "failed": 1,
    }


# ------------------------------------------------------------------ lifecycle


async def test_lifecycle_transitions_and_edit_rules(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime, ctx: dict[str, Any]
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 3)
    campaign = await _campaign(client, owner, agent)
    url = f"/api/v1/campaigns/{campaign['id']}"
    for action in ("pause", "resume", "cancel"):
        r = await client.post(f"{url}/{action}", headers=owner.headers)
        if action == "cancel":
            assert r.status_code == 200
            r = await client.post(f"{url}/resume", headers=owner.headers)
        assert r.status_code == 409, action
        assert r.json()["error"]["code"] == "invalid_state_transition"
    assert (await client.post(f"{url}/start", headers=owner.headers)).status_code == 409
    assert (await client.delete(url, headers=owner.headers)).status_code == 204

    campaign = await _started(client, owner, agent, max_concurrency=1)
    url = f"/api/v1/campaigns/{campaign['id']}"
    again = await client.post(f"{url}/start", headers=owner.headers)
    assert again.status_code == 200 and again.json()["started_at"] == campaign["started_at"]
    r = await client.patch(url, headers=owner.headers, json={"agent_id": agent})
    assert r.status_code == 422 and r.json()["error"]["details"] == {"fields": ["agent_id"]}
    r = await client.patch(url, headers=owner.headers, json={"max_concurrency": 2, "name": "N"})
    assert r.status_code == 200 and r.json()["max_concurrency"] == 2
    assert (await client.delete(url, headers=owner.headers)).status_code == 409

    assert (await client.post(f"{url}/pause", headers=owner.headers)).json()["status"] == "paused"
    assert (await worker.dispatch_campaigns(ctx))["campaigns"] == 0, "paused: nothing dispatched"
    assert (await client.post(f"{url}/resume", headers=owner.headers)).json()["status"] == "running"
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 2
    canceled = (await client.post(f"{url}/cancel", headers=owner.headers)).json()
    assert canceled["status"] == "canceled" and canceled["finished_at"]
    assert (await worker.dispatch_campaigns(ctx))["campaigns"] == 0
    calls = await _calls(campaign["id"])
    assert len(calls) == 2
    # In-flight calls still get recorded after cancellation.
    for call in calls:
        await _finish_call(call.id, CallState.COMPLETED, "qualified")
    await worker.advance_campaign_leads(ctx)
    statuses = sorted(
        (i["status"], i["skip_reason"]) for i in await _items(client, owner, campaign["id"])
    )
    assert statuses == [
        ("completed", None),
        ("completed", None),
        ("skipped", "campaign_canceled"),
    ]
    assert (await _get(client, owner, campaign["id"]))["status"] == "canceled"
    assert (await client.delete(url, headers=owner.headers)).status_code == 204
    assert (await client.get(url, headers=owner.headers)).status_code == 404


async def test_scheduled_start_waits_for_start_at(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallRuntime,
    ctx: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    emitted: list[tuple[str, dict[str, Any]]],
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 1)
    start_at = datetime.now(UTC) + timedelta(hours=1)
    campaign = await _started(client, owner, agent, start_at=start_at.isoformat())
    assert campaign["status"] == "scheduled" and campaign["started_at"] is None
    assert emitted == []
    assert (await worker.dispatch_campaigns(ctx))["campaigns"] == 0
    assert (
        await client.post(f"/api/v1/campaigns/{campaign['id']}/pause", headers=owner.headers)
    ).json()["status"] == "paused"
    assert (
        await client.post(f"/api/v1/campaigns/{campaign['id']}/resume", headers=owner.headers)
    ).json()["status"] == "scheduled"

    freeze(monkeypatch, start_at + timedelta(minutes=1))
    assert (await worker.dispatch_campaigns(ctx))["dispatched"] == 1
    running = await _get(client, owner, campaign["id"])
    assert running["status"] == "running" and running["started_at"]
    assert [e for e, _ in emitted] == ["campaign.started"]


async def test_unpublished_agent_parks_running_campaign(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime, ctx: dict[str, Any]
) -> None:
    agent = await _published_agent(client, owner)
    await _leads(client, owner, 1)
    campaign = await _started(client, owner, agent)
    assert (
        await client.post(f"/api/v1/agents/{agent}/pause", headers=owner.headers)
    ).status_code == 200
    assert (await worker.dispatch_campaigns(ctx)).get("paused") == 1
    parked = await _get(client, owner, campaign["id"])
    assert parked["status"] == "paused" and "agent" in parked["last_error"]
    assert await _calls(campaign["id"]) == []
    r = await client.post(f"/api/v1/campaigns/{campaign['id']}/resume", headers=owner.headers)
    assert r.status_code == 422, "cannot resume until the agent is published again"


# ------------------------------------------------------------------ tenancy & roles


async def test_campaigns_are_tenant_isolated_and_role_gated(
    client: httpx.AsyncClient, owner: Account, runtime: CallRuntime
) -> None:
    agent = await _published_agent(client, owner)
    lead_ids = await _leads(client, owner, 2)
    campaign = await _campaign(client, owner, agent)
    url = f"/api/v1/campaigns/{campaign['id']}"

    other = await signup(client, "b@example.com", "Tenant B")
    for method, path in (
        ("GET", url),
        ("POST", f"{url}/start"),
        ("POST", f"{url}/preview"),
        ("GET", f"{url}/progress"),
        ("GET", f"{url}/leads"),
        ("DELETE", url),
    ):
        r = await client.request(method, path, headers=other.headers)
        assert r.status_code == 404, (method, path)
    assert (await client.get("/api/v1/campaigns", headers=other.headers)).json()["total"] == 0
    # Foreign lead ids and agents are invisible, not borrowable.
    r = await client.post(
        "/api/v1/campaigns",
        headers=other.headers,
        json={"name": "Steal", "agent_id": agent, "lead_ids": lead_ids},
    )
    assert r.status_code == 422

    viewer = await invite_and_join(client, owner, "v@example.com", "viewer")
    assert (await client.get(url, headers=viewer.headers)).status_code == 200
    assert (await client.post(f"{url}/preview", headers=viewer.headers)).status_code == 200
    assert (await client.post(f"{url}/start", headers=viewer.headers)).status_code == 403
    r = await client.post(
        "/api/v1/campaigns",
        headers=viewer.headers,
        json={"name": "No", "agent_id": agent, "lead_filter": {"tag": "camp"}},
    )
    assert r.status_code == 403
    listing = (
        await client.get("/api/v1/campaigns", headers=owner.headers, params={"status": "draft"})
    ).json()
    assert listing["total"] == 1 and listing["items"][0]["calling_hours"]["mon"] == [
        ["00:00", "24:00"]
    ]
