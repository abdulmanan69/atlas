"""Call analytics: totals, rates (null vs zero), series, breakdowns, filters, tenant isolation.

Calls are seeded straight through the models so every state, outcome and mode can be
combined freely without driving the voice runtime.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.db.session import get_sessionmaker
from app.models.calls import Call, CallDirection, CallMode, CallState
from app.models.campaigns import Campaign
from app.models.voice import Agent, AgentDirection, AgentStatus
from tests.conftest import Account, signup

URL = "/api/v1/analytics/calls-overview"


async def _overview(client: httpx.AsyncClient, acct: Account, **params: object) -> dict[str, Any]:
    r = await client.get(URL, headers=acct.headers, params={k: str(v) for k, v in params.items()})
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def _call(org: uuid.UUID, **fields: Any) -> Call:
    base: dict[str, Any] = {
        "organization_id": org,
        "direction": CallDirection.OUTBOUND,
        "mode": CallMode.SIMULATED,
        "state": CallState.QUEUED,
    }
    return Call(**{**base, **fields})


async def _seed(org_id: str) -> dict[str, str]:
    """Two agents, one campaign and nine calls covering every state, outcome and mode."""
    org = uuid.UUID(org_id)
    now = datetime.now(UTC)
    async with get_sessionmaker()() as db, db.begin():
        qualifier = Agent(
            organization_id=org,
            name="Qualifier",
            direction=AgentDirection.OUTBOUND,
            status=AgentStatus.ACTIVE,
        )
        setter = Agent(
            organization_id=org,
            name="Setter",
            direction=AgentDirection.OUTBOUND,
            status=AgentStatus.ACTIVE,
        )
        db.add_all([qualifier, setter])
        await db.flush()
        campaign = Campaign(organization_id=org, name="October", agent_id=qualifier.id)
        db.add(campaign)
        await db.flush()
        a, b, c = qualifier.id, setter.id, campaign.id
        db.add_all(
            [
                # Qualifier, all inside the campaign.
                _call(
                    org,
                    agent_id=a,
                    campaign_id=c,
                    mode=CallMode.LIVE,
                    state=CallState.COMPLETED,
                    outcome="qualified",
                    duration_seconds=60,
                    estimated_cost=0.5,
                ),
                _call(
                    org,
                    agent_id=a,
                    campaign_id=c,
                    state=CallState.COMPLETED,
                    outcome="not_interested",
                    duration_seconds=30,
                ),
                _call(
                    org,
                    agent_id=a,
                    campaign_id=c,
                    state=CallState.NO_ANSWER,
                    estimated_cost=0.1,
                ),
                _call(
                    org,
                    agent_id=a,
                    campaign_id=c,
                    state=CallState.COMPLETED,
                    outcome=None,
                    duration_seconds=90,
                    opted_out=True,
                ),
                # Setter, ad hoc calls.
                _call(org, agent_id=b, state=CallState.BUSY),
                _call(org, agent_id=b, state=CallState.QUEUED),
                _call(org, agent_id=b, mode=CallMode.TEST, state=CallState.FAILED),
                # Inbound call from an unknown number: no agent, no campaign.
                _call(
                    org,
                    direction=CallDirection.INBOUND,
                    mode=CallMode.LIVE,
                    state=CallState.CANCELED,
                ),
                # Outside the default 30-day range.
                _call(
                    org,
                    agent_id=a,
                    state=CallState.COMPLETED,
                    outcome="qualified",
                    duration_seconds=10,
                    created_at=now - timedelta(days=40),
                ),
            ]
        )
    return {"qualifier": str(a), "setter": str(b), "campaign": str(c)}


async def test_empty_organization_reports_nulls_not_zeros(
    client: httpx.AsyncClient, owner: Account
) -> None:
    o = await _overview(client, owner)
    assert o["range_days"] == 30
    assert o["totals"] == {
        "calls": 0,
        "answered": 0,
        "no_answer": 0,
        "busy": 0,
        "failed": 0,
        "canceled": 0,
        "in_progress": 0,
        "attempted": 0,
        "qualified": 0,
    }
    assert o["answer_rate"] is None and o["qualification_rate"] is None
    assert o["avg_duration_seconds"] is None and o["estimated_cost"] is None
    assert o["total_duration_seconds"] == 0 and o["opted_out"] == 0
    assert len(o["daily"]) == 30 and all(
        d == {**d, "attempted": 0, "answered": 0} for d in o["daily"]
    )
    assert o["outcomes"] == [] and o["by_agent"] == [] and o["by_campaign"] == []
    assert o["mode_split"] == {"live": 0, "simulated": 0, "test": 0}


async def test_metrics_over_a_mixed_history(client: httpx.AsyncClient, owner: Account) -> None:
    ids = await _seed(owner.org_id)
    o = await _overview(client, owner)

    assert o["totals"] == {
        "calls": 8,
        "answered": 3,
        "no_answer": 1,
        "busy": 1,
        "failed": 1,
        "canceled": 1,
        "in_progress": 1,
        "attempted": 7,
        "qualified": 1,
    }
    assert o["answer_rate"] == round(3 / 7, 4)
    assert o["qualification_rate"] == round(1 / 3, 4)
    assert o["avg_duration_seconds"] == 60.0
    assert o["total_duration_seconds"] == 180
    assert o["estimated_cost"] == 0.6
    assert o["opted_out"] == 1
    assert o["mode_split"] == {"live": 2, "simulated": 5, "test": 1}

    assert len(o["daily"]) == 30
    assert sum(d["attempted"] for d in o["daily"]) == 7
    assert sum(d["answered"] for d in o["daily"]) == 3
    assert o["daily"][-1]["attempted"] == 7, "today, in the organization's timezone (UTC)"

    assert o["outcomes"] == [
        {"outcome": "not_interested", "count": 1},
        {"outcome": "qualified", "count": 1},
        {"outcome": None, "count": 1},
    ]
    assert o["by_agent"] == [
        {
            "agent_id": ids["qualifier"],
            "agent_name": "Qualifier",
            "attempted": 4,
            "answered": 3,
            "qualified": 1,
            "avg_duration_seconds": 60.0,
            "response_p50_ms": None,
        },
        {
            "agent_id": ids["setter"],
            "agent_name": "Setter",
            "attempted": 2,
            "answered": 0,
            "qualified": 0,
            "avg_duration_seconds": None,
            "response_p50_ms": None,
        },
        {
            "agent_id": None,
            "agent_name": None,
            "attempted": 1,
            "answered": 0,
            "qualified": 0,
            "avg_duration_seconds": None,
            "response_p50_ms": None,
        },
    ]
    assert o["by_campaign"] == [
        {
            "campaign_id": ids["campaign"],
            "name": "October",
            "attempted": 4,
            "answered": 3,
            "qualified": 1,
        }
    ]


async def test_filters_and_range(client: httpx.AsyncClient, owner: Account) -> None:
    ids = await _seed(owner.org_id)

    setter = await _overview(client, owner, agent_id=ids["setter"])
    assert setter["totals"]["calls"] == 3 and setter["totals"]["attempted"] == 2
    assert setter["answer_rate"] == 0.0, "attempted but never answered is zero, not unknown"
    assert setter["qualification_rate"] is None, "nothing answered: rate is undefined"
    assert setter["avg_duration_seconds"] is None
    assert [a["agent_id"] for a in setter["by_agent"]] == [ids["setter"]]
    assert setter["by_campaign"] == []

    live = await _overview(client, owner, mode="live")
    assert live["totals"]["calls"] == 2 and live["totals"]["answered"] == 1
    assert live["answer_rate"] == 0.5
    assert live["mode_split"] == {"live": 2, "simulated": 0, "test": 0}

    campaign = await _overview(client, owner, campaign_id=ids["campaign"])
    assert campaign["totals"]["calls"] == 4 and campaign["qualification_rate"] == round(1 / 3, 4)

    wide = await _overview(client, owner, range_days=60)
    assert wide["totals"]["calls"] == 9 and wide["totals"]["qualified"] == 2
    assert len(wide["daily"]) == 60

    assert (
        await client.get(URL, headers=owner.headers, params={"range_days": 0})
    ).status_code == 422
    assert (
        await client.get(URL, headers=owner.headers, params={"mode": "loud"})
    ).status_code == 422


async def test_rates_are_null_until_defined(client: httpx.AsyncClient, owner: Account) -> None:
    org = uuid.UUID(owner.org_id)
    async with get_sessionmaker()() as db, db.begin():
        db.add(_call(org, state=CallState.DIALING))
    o = await _overview(client, owner)
    assert o["totals"]["calls"] == 1 and o["totals"]["in_progress"] == 1
    assert o["answer_rate"] is None and o["qualification_rate"] is None
    assert o["estimated_cost"] is None and o["total_duration_seconds"] == 0

    async with get_sessionmaker()() as db, db.begin():
        db.add(_call(org, state=CallState.COMPLETED, outcome="callback", duration_seconds=12))
    o = await _overview(client, owner)
    assert o["answer_rate"] == 1.0 and o["qualification_rate"] == 0.0
    assert o["avg_duration_seconds"] == 12.0 and o["estimated_cost"] is None


async def test_tenant_isolation(client: httpx.AsyncClient, owner: Account) -> None:
    ids = await _seed(owner.org_id)
    other = await signup(client, "other@example.com", org="Other Co")
    o = await _overview(client, other)
    assert o["totals"]["calls"] == 0 and o["by_agent"] == [] and o["by_campaign"] == []
    # Another tenant's ids used as filters reveal nothing.
    o = await _overview(client, other, agent_id=ids["qualifier"], campaign_id=ids["campaign"])
    assert o["totals"]["calls"] == 0
    # The owner still sees everything.
    assert (await _overview(client, owner))["totals"]["calls"] == 8


async def test_call_list_carries_lead_name_and_captured_count(
    client: httpx.AsyncClient, owner: Account
) -> None:
    r = await client.post(
        "/api/v1/leads",
        headers=owner.headers,
        json={"first_name": "Hina", "last_name": "Khan", "phone": "+14155550100"},
    )
    assert r.status_code == 201, r.text
    lead_id = r.json()["id"]
    org = uuid.UUID(owner.org_id)
    async with get_sessionmaker()() as db, db.begin():
        db.add(
            _call(
                org,
                lead_id=uuid.UUID(lead_id),
                state=CallState.COMPLETED,
                outcome="qualified",
                captured={"budget": "500k", "area": "Clifton"},
            )
        )
        db.add(_call(org, state=CallState.FAILED))

    listing = (await client.get("/api/v1/calls", headers=owner.headers)).json()
    by_lead = {c["lead_id"]: c for c in listing["items"]}
    assert by_lead[lead_id]["lead_name"] == "Hina Khan"
    assert by_lead[lead_id]["captured_count"] == 2
    assert by_lead[None]["lead_name"] is None and by_lead[None]["captured_count"] == 0

    detail = (
        await client.get(f"/api/v1/calls/{by_lead[lead_id]['id']}", headers=owner.headers)
    ).json()
    assert detail["lead_name"] == "Hina Khan" and detail["captured"] == {
        "budget": "500k",
        "area": "Clifton",
    }
