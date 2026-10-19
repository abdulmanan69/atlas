"""Campaigns: CRUD, audience snapshot, lifecycle, calling hours, progress.

The worker (`app.workers.campaigns`) dispatches and reconciles; the rules it needs
(clock, calling windows, skip reasons, activation/completion, stats) live here so
the API and the scheduler never disagree. Calls are only ever placed through
`app.services.calls.start_outbound_call`.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, Select, false, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import (
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.permissions import Permission
from app.db.pagination import page_rows, page_scalars
from app.models.calls import TERMINAL_STATES, Call, CallState
from app.models.campaigns import (
    TERMINAL_STATUSES,
    WEEKDAYS,
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignMode,
    CampaignStatus,
    transition,
)
from app.models.crm import ConsentStatus, Lead
from app.models.voice import Agent, AgentStatus
from app.schemas.campaigns import (
    CampaignCreate,
    CampaignPreview,
    CampaignProgress,
    CampaignUpdate,
    LeadFilterSpec,
    LeadStatusCounts,
)
from app.services import audit, events
from app.services.leads import LeadFilters, _apply_filters

log = logging.getLogger(__name__)

# Skip reason codes recorded on campaign_leads.skip_reason and reported by preview.
SKIP_ARCHIVED = "archived"
SKIP_DO_NOT_CALL = "do_not_call"
SKIP_CONSENT_REVOKED = "consent_revoked"
SKIP_NO_PHONE = "no_phone"
SKIP_CANCELED = "campaign_canceled"
SKIP_WINDOW_ENDED = "campaign_window_ended"
SKIP_USAGE_LIMIT = "usage_limit_reached"

AVERAGE_CALL_SECONDS = 150  # planning assumption until the campaign has its own average
DIAL_OVERHEAD_SECONDS = 15
INSERT_CHUNK = 1000
# Fields that define what a campaign *is*; frozen once it has left draft.
_FROZEN_AFTER_START = frozenset({"agent_id", "mode", "lead_filter", "lead_ids"})


def utcnow() -> datetime:
    """The campaign clock. Tests monkeypatch this to freeze time for the scheduler."""
    return datetime.now(UTC)


# ---------------------------------------------------------------- time zones & calling hours


def zone(name: str | None) -> ZoneInfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def resolve_zone(*names: str | None) -> ZoneInfo:
    """First valid zone of lead → campaign → organization; UTC as the last resort."""
    for name in names:
        if (z := zone(name)) is not None:
            return z
    return ZoneInfo("UTC")


def _parse_time(value: str) -> time:
    return time.max if value == "24:00" else time.fromisoformat(value)


def _windows(hours: dict[str, Any], day: str) -> list[tuple[time, time]]:
    out: list[tuple[time, time]] = []
    for item in hours.get(day) or []:
        try:
            start, end = item
            out.append((_parse_time(str(start)), _parse_time(str(end))))
        except (TypeError, ValueError):
            continue
    return out


def within_calling_hours(hours: dict[str, Any], at: datetime, tz: ZoneInfo) -> bool:
    local = at.astimezone(tz)
    return any(s <= local.time() < e for s, e in _windows(hours, WEEKDAYS[local.weekday()]))


def next_calling_window(hours: dict[str, Any], at: datetime, tz: ZoneInfo) -> datetime | None:
    """First instant at or after `at` that falls inside a window (UTC); looks 8 days ahead."""
    local = at.astimezone(tz)
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        for start, end in _windows(hours, WEEKDAYS[day.weekday()]):
            opens = datetime.combine(day, start, tzinfo=tz)
            closes = datetime.combine(day, end, tzinfo=tz)
            if local < closes:
                return max(opens, local).astimezone(UTC)
    return None


# ---------------------------------------------------------------- eligibility


def skip_reason(
    *,
    deleted_at: datetime | None,
    do_not_call: bool,
    consent_status: ConsentStatus,
    phone_e164: str | None,
) -> str | None:
    """Reason code a lead would be skipped for, mirroring the lead checks in the call core.

    This only explains audiences; the call core re-checks every lead at dial time.
    """
    if deleted_at is not None:
        return SKIP_ARCHIVED
    if do_not_call:
        return SKIP_DO_NOT_CALL
    if consent_status == ConsentStatus.REVOKED:
        return SKIP_CONSENT_REVOKED
    if not phone_e164:
        return SKIP_NO_PHONE
    return None


def skip_reason_for(lead: Lead) -> str | None:
    return skip_reason(
        deleted_at=lead.deleted_at,
        do_not_call=lead.do_not_call,
        consent_status=lead.consent_status,
        phone_e164=lead.phone_e164,
    )


def audience_query(campaign: Campaign) -> Select[tuple[Lead]]:
    """Live leads of the campaign's organization matching the saved filter or the id list."""
    base = select(Lead).where(
        Lead.organization_id == campaign.organization_id, Lead.deleted_at.is_(None)
    )
    conditions: list[ColumnElement[bool]] = []
    if campaign.lead_filter:
        spec = LeadFilterSpec.model_validate(campaign.lead_filter)
        if not spec.is_empty():
            clause = _apply_filters(select(Lead), LeadFilters(**spec.model_dump())).whereclause
            if clause is not None:
                conditions.append(clause)
    if campaign.lead_ids:
        conditions.append(Lead.id.in_(campaign.lead_ids))
    if not conditions:
        return base.where(false())
    return base.where(or_(*conditions))


def estimate_seconds(n_calls: int, campaign: Campaign, avg_call_seconds: float) -> int:
    """Time to place `n_calls` given concurrency and rate (first attempts only)."""
    if n_calls <= 0:
        return 0
    by_concurrency = campaign.max_concurrency / (avg_call_seconds + DIAL_OVERHEAD_SECONDS)
    by_rate = campaign.calls_per_minute / 60
    return int(n_calls / min(by_concurrency, by_rate))


# ---------------------------------------------------------------- CRUD


async def list_campaigns(
    db: AsyncSession,
    ctx: TenantContext,
    *,
    status: CampaignStatus | None,
    page: int,
    page_size: int,
) -> tuple[list[Campaign], int]:
    stmt = select(Campaign).where(Campaign.organization_id == ctx.org_id)
    if status:
        stmt = stmt.where(Campaign.status == status)
    return await page_scalars(
        db, stmt.order_by(Campaign.created_at.desc(), Campaign.id), page, page_size
    )


async def get_campaign(db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID) -> Campaign:
    campaign = (
        await db.execute(
            select(Campaign).where(
                Campaign.id == campaign_id, Campaign.organization_id == ctx.org_id
            )
        )
    ).scalar_one_or_none()
    if campaign is None:
        raise NotFoundError("Campaign not found")
    return campaign


async def _agent_in_org(db: AsyncSession, org_id: uuid.UUID, agent_id: uuid.UUID) -> Agent:
    agent = (
        await db.execute(select(Agent).where(Agent.id == agent_id, Agent.organization_id == org_id))
    ).scalar_one_or_none()
    if agent is None or agent.status == AgentStatus.ARCHIVED:
        raise ValidationFailedError("Choose an agent that exists and is not archived")
    return agent


def agent_can_call(agent: Agent | None) -> bool:
    return (
        agent is not None
        and agent.status == AgentStatus.ACTIVE
        and agent.published_version is not None
    )


async def _runnable_agent(db: AsyncSession, campaign: Campaign) -> Agent:
    agent = await _agent_in_org(db, campaign.organization_id, campaign.agent_id)
    if not agent_can_call(agent):
        raise ValidationFailedError("Publish the agent and make sure it is active before starting")
    return agent


async def _audit(
    db: AsyncSession, ctx: TenantContext | None, campaign: Campaign, action: str, **changes: Any
) -> None:
    await audit.record(
        db,
        action=action,
        organization_id=campaign.organization_id,
        actor_user_id=ctx.user_id if ctx else None,
        resource_type="campaign",
        resource_id=campaign.id,
        changes=changes,
        ip=ctx.meta.ip if ctx else None,
        user_agent=ctx.meta.user_agent if ctx else None,
    )


async def create_campaign(db: AsyncSession, ctx: TenantContext, data: CampaignCreate) -> Campaign:
    await _agent_in_org(db, ctx.org_id, data.agent_id)
    campaign = Campaign(
        organization_id=ctx.org_id,
        name=data.name,
        objective=data.objective,
        agent_id=data.agent_id,
        mode=data.mode,
        status=CampaignStatus.DRAFT,
        lead_filter=data.lead_filter.model_dump(mode="json") if data.lead_filter else None,
        lead_ids=data.lead_ids or None,
        start_at=data.start_at,
        end_at=data.end_at,
        timezone=data.timezone,
        calling_hours=data.calling_hours.model_dump(mode="json"),
        max_concurrency=data.max_concurrency,
        calls_per_minute=data.calls_per_minute,
        retry_policy=data.retry_policy.model_dump(mode="json"),
        max_calls=data.max_calls,
        telephony_configuration_id=data.telephony_configuration_id,
        from_phone_number_id=data.from_phone_number_id,
        created_by_user_id=ctx.user_id,
    )
    db.add(campaign)
    await db.flush()
    await _audit(db, ctx, campaign, "campaign.created", name=data.name, mode=data.mode.value)
    await db.flush()
    await db.refresh(campaign)
    return campaign


async def update_campaign(
    db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID, data: CampaignUpdate
) -> Campaign:
    campaign = await get_campaign(db, ctx, campaign_id)
    if campaign.status in TERMINAL_STATUSES:
        raise InvalidStateTransitionError(
            f"A {campaign.status.value} campaign can no longer be edited"
        )
    changes = data.model_dump(exclude_unset=True)
    if campaign.status != CampaignStatus.DRAFT:
        frozen = sorted(_FROZEN_AFTER_START & changes.keys())
        if frozen:
            raise ValidationFailedError(
                "The agent, mode and audience cannot change once a campaign has started",
                details={"fields": frozen},
            )
    if "agent_id" in changes:
        await _agent_in_org(db, ctx.org_id, changes["agent_id"])
    for field in ("lead_filter", "calling_hours", "retry_policy"):
        if field in changes and changes[field] is not None:
            model = getattr(data, field)
            changes[field] = model.model_dump(mode="json")
    if "lead_ids" in changes and not changes["lead_ids"]:
        changes["lead_ids"] = None
    tracked: dict[str, Any] = {}
    for field, value in changes.items():
        if getattr(campaign, field) != value:
            setattr(campaign, field, value)
            tracked[field] = "changed"
    has_filter = (
        campaign.lead_filter is not None
        and not LeadFilterSpec.model_validate(campaign.lead_filter).is_empty()
    )
    if not has_filter and not campaign.lead_ids:
        raise ValidationFailedError("Choose an audience: a lead filter and/or explicit lead ids")
    if campaign.start_at and campaign.end_at and campaign.end_at <= campaign.start_at:
        raise ValidationFailedError("end_at must be after start_at")
    if tracked:
        await _audit(db, ctx, campaign, "campaign.updated", fields=sorted(tracked))
    await db.flush()
    await db.refresh(campaign)
    return campaign


async def delete_campaign(db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID) -> None:
    campaign = await get_campaign(db, ctx, campaign_id)
    if campaign.status not in TERMINAL_STATUSES and campaign.status != CampaignStatus.DRAFT:
        raise InvalidStateTransitionError("Cancel the campaign before deleting it")
    await _audit(db, ctx, campaign, "campaign.deleted", name=campaign.name)
    await db.delete(campaign)
    await db.flush()


# ---------------------------------------------------------------- audience


async def preview(db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID) -> CampaignPreview:
    """Counts of who would be called if the campaign started now. No side effects."""
    campaign = await get_campaign(db, ctx, campaign_id)
    revoked = Lead.consent_status == ConsentStatus.REVOKED
    counts = (
        await db.execute(
            audience_query(campaign).with_only_columns(
                func.count(),
                func.count().filter(Lead.do_not_call.is_(True)),
                func.count().filter(Lead.do_not_call.is_(False), revoked),
                func.count().filter(
                    Lead.do_not_call.is_(False), ~revoked, Lead.phone_e164.is_(None)
                ),
            )
        )
    ).one()
    matched, dnc, consent, no_phone = (int(c or 0) for c in counts)
    skipped = {
        k: v
        for k, v in (
            (SKIP_DO_NOT_CALL, dnc),
            (SKIP_CONSENT_REVOKED, consent),
            (SKIP_NO_PHONE, no_phone),
        )
        if v
    }
    eligible = matched - dnc - consent - no_phone
    capped = campaign.max_calls is not None and eligible > campaign.max_calls
    planned = min(eligible, campaign.max_calls) if campaign.max_calls else eligible
    now = utcnow()
    tz = resolve_zone(campaign.timezone, ctx.organization.timezone)
    return CampaignPreview(
        matched=matched,
        eligible=eligible,
        skipped=skipped,
        capped_by_max_calls=capped,
        estimated_duration_seconds=estimate_seconds(planned, campaign, AVERAGE_CALL_SECONDS),
        calling_window_open=within_calling_hours(campaign.calling_hours, now, tz),
        next_window_opens_at=next_calling_window(campaign.calling_hours, now, tz),
    )


async def build_audience(db: AsyncSession, campaign: Campaign) -> dict[str, Any]:
    """Snapshot the audience into campaign_leads. Idempotent: existing rows are kept."""
    rows = (
        await db.execute(
            audience_query(campaign).with_only_columns(
                Lead.id, Lead.deleted_at, Lead.do_not_call, Lead.consent_status, Lead.phone_e164
            )
        )
    ).all()
    skipped: dict[str, int] = {}
    values: list[dict[str, Any]] = []
    for lead_id, deleted_at, dnc, consent, phone in rows:
        reason = skip_reason(
            deleted_at=deleted_at, do_not_call=dnc, consent_status=consent, phone_e164=phone
        )
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        values.append(
            {
                "id": uuid.uuid4(),
                "organization_id": campaign.organization_id,
                "campaign_id": campaign.id,
                "lead_id": lead_id,
                "status": CampaignLeadStatus.SKIPPED if reason else CampaignLeadStatus.PENDING,
                "skip_reason": reason,
                "attempts": 0,
            }
        )
    for i in range(0, len(values), INSERT_CHUNK):
        await db.execute(
            insert(CampaignLead)
            .values(values[i : i + INSERT_CHUNK])
            .on_conflict_do_nothing(index_elements=["campaign_id", "lead_id"])
        )
    eligible = int(
        await db.scalar(
            select(func.count())
            .select_from(CampaignLead)
            .where(
                CampaignLead.campaign_id == campaign.id,
                CampaignLead.status != CampaignLeadStatus.SKIPPED,
            )
        )
        or 0
    )
    return {"matched": len(rows), "eligible": eligible, "skipped": skipped}


# ---------------------------------------------------------------- lifecycle


async def activate(
    db: AsyncSession, campaign: Campaign, now: datetime, *, actor: TenantContext | None
) -> None:
    """Move to running; emits campaign.started the first time the campaign runs."""
    transition(campaign, CampaignStatus.RUNNING)
    campaign.backoff_until = None
    campaign.last_error = None
    first_run = campaign.started_at is None
    campaign.started_at = campaign.started_at or now
    await _audit(db, actor, campaign, "campaign.started", mode=campaign.mode.value)
    if first_run:
        await events.emit(
            db,
            campaign.organization_id,
            "campaign.started",
            {"campaign_id": str(campaign.id), "name": campaign.name, "mode": campaign.mode.value},
        )


async def finish(db: AsyncSession, campaign: Campaign, now: datetime) -> None:
    transition(campaign, CampaignStatus.COMPLETED)
    campaign.finished_at = now
    campaign.backoff_until = None
    campaign.stats = await compute_stats(db, campaign)
    await _audit(db, None, campaign, "campaign.completed", stats=campaign.stats)
    await events.emit(
        db,
        campaign.organization_id,
        "campaign.completed",
        {"campaign_id": str(campaign.id), "name": campaign.name, "stats": campaign.stats},
    )


async def pause_for_error(db: AsyncSession, campaign: Campaign, reason: str) -> None:
    """The worker parks a campaign it cannot safely run; a person resumes it after fixing."""
    transition(campaign, CampaignStatus.PAUSED)
    campaign.last_error = reason[:500]
    await _audit(db, None, campaign, "campaign.paused", reason=reason[:500])


async def start(db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID) -> Campaign:
    """Validate, snapshot the audience and run (or schedule). Idempotent once started."""
    campaign = await get_campaign(db, ctx, campaign_id)
    if campaign.status in (CampaignStatus.RUNNING, CampaignStatus.SCHEDULED):
        return campaign
    if campaign.status == CampaignStatus.PAUSED:
        raise InvalidStateTransitionError("Resume the paused campaign instead of starting it")
    if campaign.status != CampaignStatus.DRAFT:
        raise InvalidStateTransitionError(
            f"A {campaign.status.value} campaign cannot be started again"
        )
    now = utcnow()
    if campaign.end_at and campaign.end_at <= now:
        raise ValidationFailedError("The campaign's end time has already passed")
    await _runnable_agent(db, campaign)
    live = campaign.mode == CampaignMode.LIVE
    if live:
        # Both gates, in this order: the person must hold the permission and the
        # deployment must allow real calls. Neither alone is enough.
        ctx.require(Permission.CAMPAIGNS_LAUNCH_LIVE)
        if not get_settings().live_calls_enabled:
            raise PermissionDeniedError(
                "Live calls are disabled on this deployment (ATLAS_LIVE_CALLS_ENABLED=false)"
            )
    summary = await build_audience(db, campaign)
    if summary["eligible"] == 0:
        raise ValidationFailedError("Nobody in this audience can be called", details=summary)
    campaign.started_by_user_id = ctx.user_id
    if live:
        await _audit(
            db,
            ctx,
            campaign,
            "campaign.live_authorized",
            authorized_by=str(ctx.user_id),
            eligible=summary["eligible"],
        )
    if campaign.start_at and campaign.start_at > now:
        transition(campaign, CampaignStatus.SCHEDULED)
        await _audit(
            db, ctx, campaign, "campaign.scheduled", start_at=campaign.start_at.isoformat()
        )
    else:
        await activate(db, campaign, now, actor=ctx)
    campaign.stats = await compute_stats(db, campaign)
    await db.flush()
    await db.refresh(campaign)
    return campaign


async def pause(db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID) -> Campaign:
    campaign = await get_campaign(db, ctx, campaign_id)
    transition(campaign, CampaignStatus.PAUSED)
    await _audit(db, ctx, campaign, "campaign.paused")
    await db.flush()
    await db.refresh(campaign)
    return campaign


async def resume(db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID) -> Campaign:
    campaign = await get_campaign(db, ctx, campaign_id)
    if campaign.status != CampaignStatus.PAUSED:
        raise InvalidStateTransitionError("Only a paused campaign can be resumed")
    now = utcnow()
    if campaign.end_at and campaign.end_at <= now:
        raise ValidationFailedError("The campaign's end time has already passed")
    await _runnable_agent(db, campaign)
    if campaign.mode == CampaignMode.LIVE:
        ctx.require(Permission.CAMPAIGNS_LAUNCH_LIVE)
        if not get_settings().live_calls_enabled:
            raise PermissionDeniedError(
                "Live calls are disabled on this deployment (ATLAS_LIVE_CALLS_ENABLED=false)"
            )
    if campaign.start_at and campaign.start_at > now:
        transition(campaign, CampaignStatus.SCHEDULED)
        campaign.last_error = None
        await _audit(db, ctx, campaign, "campaign.scheduled")
    else:
        await activate(db, campaign, now, actor=ctx)
    await db.flush()
    await db.refresh(campaign)
    return campaign


async def cancel(db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID) -> Campaign:
    """Stop dispatching; calls already in progress run to completion and are recorded."""
    campaign = await get_campaign(db, ctx, campaign_id)
    transition(campaign, CampaignStatus.CANCELED)
    campaign.finished_at = utcnow()
    campaign.backoff_until = None
    await db.execute(
        update(CampaignLead)
        .where(
            CampaignLead.campaign_id == campaign.id,
            CampaignLead.status == CampaignLeadStatus.PENDING,
        )
        .values(status=CampaignLeadStatus.SKIPPED, skip_reason=SKIP_CANCELED)
    )
    campaign.stats = await compute_stats(db, campaign)
    await _audit(db, ctx, campaign, "campaign.canceled")
    await db.flush()
    await db.refresh(campaign)
    return campaign


# ---------------------------------------------------------------- progress


async def compute_stats(db: AsyncSession, campaign: Campaign) -> dict[str, Any]:
    """Counters cached on the campaign row and returned by the progress endpoint."""
    lead_rows = await db.execute(
        select(CampaignLead.status, func.count())
        .where(CampaignLead.campaign_id == campaign.id)
        .group_by(CampaignLead.status)
    )
    leads = {status.value: int(n) for status, n in lead_rows.all()}
    state_rows = await db.execute(
        select(Call.state, func.count())
        .where(Call.organization_id == campaign.organization_id, Call.campaign_id == campaign.id)
        .group_by(Call.state)
    )
    by_state = {state.value: int(n) for state, n in state_rows.all()}
    outcome_rows = await db.execute(
        select(Call.outcome, func.count())
        .where(
            Call.organization_id == campaign.organization_id,
            Call.campaign_id == campaign.id,
            Call.outcome.is_not(None),
        )
        .group_by(Call.outcome)
    )
    by_outcome = {str(outcome): int(n) for outcome, n in outcome_rows.all()}
    avg = await db.scalar(
        select(func.avg(Call.duration_seconds)).where(
            Call.organization_id == campaign.organization_id,
            Call.campaign_id == campaign.id,
            Call.state == CallState.COMPLETED,
        )
    )
    finished = sum(by_state.get(s.value, 0) for s in TERMINAL_STATES)
    answered = by_state.get(CallState.COMPLETED.value, 0)
    remaining = leads.get("pending", 0) + leads.get("calling", 0)
    eta = (
        estimate_seconds(remaining, campaign, float(avg) if avg else AVERAGE_CALL_SECONDS)
        if campaign.status == CampaignStatus.RUNNING
        else None
    )
    return {
        "leads": leads,
        "calls": {"total": sum(by_state.values()), "by_state": by_state, "by_outcome": by_outcome},
        "answer_rate": round(answered / finished, 4) if finished else None,
        "average_duration_seconds": int(avg) if avg else None,
        "eta_seconds": eta,
        "updated_at": utcnow().isoformat(),
    }


def progress_from(campaign: Campaign, stats: dict[str, Any]) -> CampaignProgress:
    calls = stats.get("calls") or {}
    return CampaignProgress(
        status=campaign.status,
        leads=LeadStatusCounts.model_validate(stats.get("leads") or {}),
        calls_total=int(calls.get("total") or 0),
        calls_by_state=dict(calls.get("by_state") or {}),
        calls_by_outcome=dict(calls.get("by_outcome") or {}),
        answer_rate=stats.get("answer_rate"),
        average_duration_seconds=stats.get("average_duration_seconds"),
        started_at=campaign.started_at,
        finished_at=campaign.finished_at,
        backoff_until=campaign.backoff_until,
        eta_seconds=stats.get("eta_seconds"),
    )


async def progress(
    db: AsyncSession, ctx: TenantContext, campaign_id: uuid.UUID
) -> CampaignProgress:
    campaign = await get_campaign(db, ctx, campaign_id)
    return progress_from(campaign, await compute_stats(db, campaign))


async def list_campaign_leads(
    db: AsyncSession,
    ctx: TenantContext,
    campaign_id: uuid.UUID,
    *,
    status: CampaignLeadStatus | None,
    page: int,
    page_size: int,
) -> tuple[list[tuple[CampaignLead, Lead | None]], int]:
    campaign = await get_campaign(db, ctx, campaign_id)
    stmt = (
        select(CampaignLead, Lead)
        .join(Lead, Lead.id == CampaignLead.lead_id, isouter=True)
        .where(CampaignLead.campaign_id == campaign.id)
    )
    if status:
        stmt = stmt.where(CampaignLead.status == status)
    rows, total = await page_rows(
        db, stmt.order_by(CampaignLead.created_at, CampaignLead.id), page, page_size
    )
    return [(cl, lead) for cl, lead in rows], total
