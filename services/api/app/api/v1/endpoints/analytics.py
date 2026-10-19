"""CRM and call analytics from persisted data. Every metric is defined in its field description."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Query
from pydantic import Field
from sqlalchemy import ColumnElement, Integer, and_, case, column, desc, func, literal, select, true
from sqlalchemy.dialects.postgresql import JSONB

from app.api.v1.deps import DB, AnalyticsRead
from app.models.calls import TERMINAL_STATES, Call, CallMode, CallState
from app.models.campaigns import Campaign
from app.models.crm import Lead, PipelineStage, StageCategory, Task, TaskStatus
from app.models.voice import Agent
from app.schemas.common import ERROR_RESPONSES, APIModel

router = APIRouter(prefix="/analytics", tags=["Analytics"], responses=ERROR_RESPONSES)


class StageCount(APIModel):
    stage_id: uuid.UUID
    key: str
    name: str
    category: StageCategory
    position: int
    count: int


class DailyCount(APIModel):
    date: date
    count: int


class SourceCount(APIModel):
    source: str | None = Field(description="null = lead created without a source")
    count: int


class CrmOverview(APIModel):
    range_days: int
    total_leads: int = Field(description="Live (not archived) leads, all time")
    new_leads: int = Field(description="Leads created within the range")
    new_leads_previous: int = Field(description="Leads created in the equal-length prior range")
    converted_leads: int = Field(description="Live leads currently in a 'won' stage")
    conversion_rate: float | None = Field(
        description="converted_leads / total_leads; null when there are no leads"
    )
    do_not_call: int = Field(description="Live leads flagged do-not-call")
    unassigned: int = Field(description="Live leads without an owner")
    open_tasks: int
    overdue_tasks: int = Field(description="Open tasks whose due time has passed")
    follow_ups_due: int = Field(description="Live leads with next_follow_up_at in the past")
    by_stage: list[StageCount]
    by_source: list[SourceCount]
    daily_new_leads: list[DailyCount] = Field(
        description="Leads created per calendar day in the organization's timezone; zero-filled"
    )


@router.get("/crm-overview", response_model=CrmOverview, summary="CRM pipeline overview")
async def crm_overview(
    ctx: AnalyticsRead,
    db: DB,
    range_days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> CrmOverview:
    org = ctx.org_id
    now = datetime.now(UTC)
    start = now - timedelta(days=range_days)
    prev_start = start - timedelta(days=range_days)
    live = and_(Lead.organization_id == org, Lead.deleted_at.is_(None))

    totals = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(Lead.created_at >= start),
                func.count().filter(and_(Lead.created_at >= prev_start, Lead.created_at < start)),
                func.count().filter(Lead.do_not_call.is_(True)),
                func.count().filter(Lead.owner_user_id.is_(None)),
                func.count().filter(Lead.next_follow_up_at < now),
            ).where(live)
        )
    ).one()
    total, new, new_prev, dnc, unassigned, follow_ups = (int(v) for v in totals)

    stage_rows = (
        await db.execute(
            select(PipelineStage, func.count(Lead.id))
            .outerjoin(Lead, and_(Lead.stage_id == PipelineStage.id, live))
            .where(PipelineStage.organization_id == org)
            .group_by(PipelineStage.id)
            .order_by(PipelineStage.position)
        )
    ).all()
    by_stage = [
        StageCount(
            stage_id=s.id,
            key=s.key,
            name=s.name,
            category=s.category,
            position=s.position,
            count=int(c),
        )
        for s, c in stage_rows
    ]
    converted = sum(s.count for s in by_stage if s.category == StageCategory.WON)

    source_rows = (
        await db.execute(
            select(Lead.source, func.count())
            .where(live)
            .group_by(Lead.source)
            .order_by(func.count().desc())
            .limit(8)
        )
    ).all()

    tz = ZoneInfo(ctx.organization.timezone)
    local_day = func.date(func.timezone(ctx.organization.timezone, Lead.created_at))
    day_rows = (
        await db.execute(
            select(local_day, func.count())
            .where(live, Lead.created_at >= start)
            .group_by(local_day)
        )
    ).all()
    per_day = {d: int(c) for d, c in day_rows}
    today = now.astimezone(tz).date()
    daily = [
        DailyCount(date=d, count=per_day.get(d, 0))
        for d in (today - timedelta(days=i) for i in range(range_days - 1, -1, -1))
    ]

    task_rows = (
        await db.execute(
            select(func.count(), func.count().filter(Task.due_at < now)).where(
                Task.organization_id == org, Task.status == TaskStatus.OPEN
            )
        )
    ).one()

    return CrmOverview(
        range_days=range_days,
        total_leads=total,
        new_leads=new,
        new_leads_previous=new_prev,
        converted_leads=converted,
        conversion_rate=round(converted / total, 4) if total else None,
        do_not_call=dnc,
        unassigned=unassigned,
        open_tasks=int(task_rows[0]),
        overdue_tasks=int(task_rows[1]),
        follow_ups_due=follow_ups,
        by_stage=by_stage,
        by_source=[SourceCount(source=s, count=int(c)) for s, c in source_rows],
        daily_new_leads=daily,
    )


# --------------------------------------------------------------------------- calls


class CallTotals(APIModel):
    calls: int = Field(description="Call records created in the range, in any state")
    answered: int = Field(description="Calls in state completed: the person answered")
    no_answer: int = Field(description="Calls in state no_answer")
    busy: int = Field(description="Calls in state busy")
    failed: int = Field(description="Calls in state failed")
    canceled: int = Field(description="Calls in state canceled")
    in_progress: int = Field(description="Calls not yet terminal (queued, dialing, in_progress)")
    attempted: int = Field(
        description="Calls in a terminal state (completed, no_answer, busy, failed, canceled)"
    )
    qualified: int = Field(description="Answered calls whose outcome code is 'qualified'")


class CallsDaily(APIModel):
    """One calendar day in the organization's timezone."""

    date: date
    attempted: int = Field(description="Terminal-state calls created on that day")
    answered: int = Field(description="Completed calls created on that day")


class OutcomeCount(APIModel):
    outcome: str | None = Field(description="Outcome code; null = answered without an outcome")
    count: int


class AgentCallStats(APIModel):
    agent_id: uuid.UUID | None = Field(description="null = calls with no agent (unknown inbound)")
    agent_name: str | None = Field(description="Agent name at read time; null when unknown")
    attempted: int = Field(description="Terminal-state calls")
    answered: int = Field(description="Completed calls")
    qualified: int = Field(description="Answered calls with outcome 'qualified'")
    avg_duration_seconds: float | None = Field(
        description="Mean duration of answered calls; null when none were answered"
    )
    response_p50_ms: int | None = Field(
        default=None,
        description=(
            "Median response time over every measured turn of this agent's real calls "
            "(simulated calls excluded); null when nothing was measured"
        ),
    )


class CampaignCallStats(APIModel):
    campaign_id: uuid.UUID
    name: str = Field(description="Campaign name at read time")
    attempted: int = Field(description="Terminal-state calls placed by the campaign")
    answered: int = Field(description="Completed calls placed by the campaign")
    qualified: int = Field(description="Answered calls with outcome 'qualified'")


class ModeSplit(APIModel):
    live: int = Field(description="Real phone calls")
    simulated: int = Field(description="Dry runs: nothing left the platform")
    test: int = Field(description="Browser test calls from the agent editor")


class CallsOverview(APIModel):
    range_days: int
    totals: CallTotals
    answer_rate: float | None = Field(
        description="answered / attempted (terminal-state calls); null when nothing was attempted"
    )
    qualification_rate: float | None = Field(
        description="qualified / answered; null when no call was answered"
    )
    avg_duration_seconds: float | None = Field(
        description="Mean duration of answered calls; null when none were answered"
    )
    total_duration_seconds: int = Field(description="Sum of durations across every call; 0 if none")
    estimated_cost: float | None = Field(
        description="Sum of per-call cost estimates; null when no call reported a cost"
    )
    opted_out: int = Field(description="Calls in which the person asked not to be contacted")
    daily: list[CallsDaily] = Field(
        description="One entry per calendar day in the organization's timezone; zero-filled"
    )
    outcomes: list[OutcomeCount] = Field(
        description="Answered calls by outcome code, most frequent first"
    )
    by_agent: list[AgentCallStats] = Field(description="Most attempted first")
    by_campaign: list[CampaignCallStats] = Field(
        description="Calls placed by a campaign, most attempted first"
    )
    mode_split: ModeSplit
    response_p50_ms: int | None = Field(
        default=None,
        description=(
            "Median time from the person finishing speaking to the agent starting to speak, "
            "over every measured turn of real calls in scope (simulated calls never count); "
            "null when nothing was measured"
        ),
    )
    response_p95_ms: int | None = Field(
        default=None, description="95th percentile of the same turns; null when none"
    )
    measured_calls: int = Field(
        default=0, description="Real calls in scope with at least one measured turn"
    )


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _avg(value: float | None) -> float | None:
    return round(float(value), 1) if value is not None else None


def _real_latency(scope: ColumnElement[bool]) -> ColumnElement[bool]:
    """Calls whose latency was measured by the voice service (never dry runs)."""
    return and_(
        scope,
        Call.mode != CallMode.SIMULATED,
        Call.latency["summary"]["source"].astext == "runtime",
    )


def _turn_responses() -> tuple[Any, ColumnElement[int]]:
    """A lateral set of each call's turns and the turn's response_ms (null if absent)."""
    turns_json = case(
        (func.jsonb_typeof(Call.latency["turns"]) == "array", Call.latency["turns"]),
        else_=literal("[]", JSONB),
    )
    turns = func.jsonb_array_elements(turns_json).table_valued(column("value", JSONB)).lateral("t")
    value = turns.c.value
    response = case(
        (
            func.jsonb_typeof(value["response_ms"]) == "number",
            value["response_ms"].astext.cast(Integer),
        )
    )
    return turns, response


def _ms(value: float | None) -> int | None:
    return round(float(value)) if value is not None else None


@router.get(
    "/calls-overview",
    response_model=CallsOverview,
    summary="Call volume, answer and qualification rates, outcomes, agents, campaigns, cost",
)
async def calls_overview(
    ctx: AnalyticsRead,
    db: DB,
    range_days: Annotated[int, Query(ge=1, le=365)] = 30,
    agent_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    mode: CallMode | None = None,
) -> CallsOverview:
    org = ctx.org_id
    now = datetime.now(UTC)
    start = now - timedelta(days=range_days)
    conditions = [Call.organization_id == org, Call.created_at >= start]
    if agent_id is not None:
        conditions.append(Call.agent_id == agent_id)
    if campaign_id is not None:
        conditions.append(Call.campaign_id == campaign_id)
    if mode is not None:
        conditions.append(Call.mode == mode)
    scope = and_(*conditions)
    terminal = Call.state.in_(TERMINAL_STATES)
    answered = Call.state == CallState.COMPLETED
    qualified = and_(answered, Call.outcome == "qualified")

    totals = (
        await db.execute(
            select(
                func.count(),
                func.count().filter(answered),
                func.count().filter(Call.state == CallState.NO_ANSWER),
                func.count().filter(Call.state == CallState.BUSY),
                func.count().filter(Call.state == CallState.FAILED),
                func.count().filter(Call.state == CallState.CANCELED),
                func.count().filter(Call.state.not_in(TERMINAL_STATES)),
                func.count().filter(terminal),
                func.count().filter(qualified),
                func.avg(Call.duration_seconds).filter(answered),
                func.coalesce(func.sum(Call.duration_seconds), 0),
                func.sum(Call.estimated_cost),
                func.count().filter(Call.opted_out.is_(True)),
                func.count().filter(Call.mode == CallMode.LIVE),
                func.count().filter(Call.mode == CallMode.SIMULATED),
                func.count().filter(Call.mode == CallMode.TEST),
            ).where(scope)
        )
    ).one()
    (
        calls,
        n_answered,
        n_no_answer,
        n_busy,
        n_failed,
        n_canceled,
        n_in_progress,
        n_attempted,
        n_qualified,
        avg_duration,
        total_duration,
        cost,
        n_opted_out,
        n_live,
        n_simulated,
        n_test,
    ) = totals

    tz = ZoneInfo(ctx.organization.timezone)
    local_day = func.date(func.timezone(ctx.organization.timezone, Call.created_at))
    day_rows = (
        await db.execute(
            select(local_day, func.count().filter(terminal), func.count().filter(answered))
            .where(scope)
            .group_by(local_day)
        )
    ).all()
    per_day = {d: (int(a), int(b)) for d, a, b in day_rows}
    today = now.astimezone(tz).date()
    daily = [
        CallsDaily(date=d, attempted=per_day.get(d, (0, 0))[0], answered=per_day.get(d, (0, 0))[1])
        for d in (today - timedelta(days=i) for i in range(range_days - 1, -1, -1))
    ]

    outcome_rows = (
        await db.execute(
            select(Call.outcome, func.count().label("n"))
            .where(scope, answered)
            .group_by(Call.outcome)
            .order_by(desc("n"), Call.outcome.asc().nulls_last())
        )
    ).all()

    agent_rows = (
        await db.execute(
            select(
                Call.agent_id,
                Agent.name,
                func.count().filter(terminal).label("attempted"),
                func.count().filter(answered),
                func.count().filter(qualified),
                func.avg(Call.duration_seconds).filter(answered),
            )
            .outerjoin(Agent, Agent.id == Call.agent_id)
            .where(scope)
            .group_by(Call.agent_id, Agent.name)
            .order_by(desc("attempted"), Agent.name.asc().nulls_last())
        )
    ).all()

    real = _real_latency(scope)
    turns, response = _turn_responses()
    latency_row = (
        await db.execute(
            select(
                func.percentile_cont(0.5).within_group(response),
                func.percentile_cont(0.95).within_group(response),
            )
            .select_from(Call)
            .join(turns, true())
            .where(real, response.is_not(None))
        )
    ).one()
    measured_calls = int(
        await db.scalar(
            select(func.count()).where(
                real, Call.latency["summary"]["measured_turns"].as_integer() > 0
            )
        )
        or 0
    )
    agent_latency = {
        aid: _ms(p50)
        for aid, p50 in (
            await db.execute(
                select(Call.agent_id, func.percentile_cont(0.5).within_group(response))
                .select_from(Call)
                .join(turns, true())
                .where(real, response.is_not(None))
                .group_by(Call.agent_id)
            )
        ).all()
    }

    campaign_rows = (
        await db.execute(
            select(
                Call.campaign_id,
                Campaign.name,
                func.count().filter(terminal).label("attempted"),
                func.count().filter(answered),
                func.count().filter(qualified),
            )
            .join(Campaign, Campaign.id == Call.campaign_id)
            .where(scope)
            .group_by(Call.campaign_id, Campaign.name)
            .order_by(desc("attempted"), Campaign.name)
        )
    ).all()

    return CallsOverview(
        range_days=range_days,
        totals=CallTotals(
            calls=int(calls),
            answered=int(n_answered),
            no_answer=int(n_no_answer),
            busy=int(n_busy),
            failed=int(n_failed),
            canceled=int(n_canceled),
            in_progress=int(n_in_progress),
            attempted=int(n_attempted),
            qualified=int(n_qualified),
        ),
        answer_rate=_ratio(int(n_answered), int(n_attempted)),
        qualification_rate=_ratio(int(n_qualified), int(n_answered)),
        avg_duration_seconds=_avg(avg_duration),
        total_duration_seconds=int(total_duration),
        estimated_cost=round(float(cost), 4) if cost is not None else None,
        opted_out=int(n_opted_out),
        daily=daily,
        outcomes=[OutcomeCount(outcome=o, count=int(n)) for o, n in outcome_rows],
        by_agent=[
            AgentCallStats(
                agent_id=aid,
                agent_name=name,
                attempted=int(att),
                answered=int(ans),
                qualified=int(q),
                avg_duration_seconds=_avg(avg),
                response_p50_ms=agent_latency.get(aid),
            )
            for aid, name, att, ans, q, avg in agent_rows
        ],
        by_campaign=[
            CampaignCallStats(
                campaign_id=cid, name=name, attempted=int(att), answered=int(ans), qualified=int(q)
            )
            for cid, name, att, ans, q in campaign_rows
        ],
        mode_split=ModeSplit(live=int(n_live), simulated=int(n_simulated), test=int(n_test)),
        response_p50_ms=_ms(latency_row[0]),
        response_p95_ms=_ms(latency_row[1]),
        measured_calls=measured_calls,
    )
