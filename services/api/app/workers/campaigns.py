"""Campaign scheduler.

Two cron jobs keep campaigns moving without any in-memory state, so any number of
workers can run them and a restart loses nothing:

* `dispatch_campaigns` (every 30 s): for each running campaign inside its window, take
  as many due leads as the concurrency and per-minute budgets allow (row locks with
  SKIP LOCKED), and hand each one to the call core. Simulated calls get a completion
  job with a short random delay so progress is observable.
* `advance_campaign_leads` (every 30 s, offset): map finished calls back onto their
  campaign leads (done / retry later / exhausted / failed), close campaigns with nothing
  left to do, and refresh the cached stats.

Every call is placed through `app.services.calls.start_outbound_call`; this module
never decides on its own whether a real call may happen.
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timedelta
from typing import Any

from arq import cron
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import IntegrationUnavailableError, PermissionDeniedError
from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.client import RuntimeCapacityError
from app.models.calls import TERMINAL_STATES, Call, CallMode, CallState
from app.models.campaigns import (
    ACTIVE_STATUSES,
    Campaign,
    CampaignLead,
    CampaignLeadStatus,
    CampaignMode,
    CampaignStatus,
)
from app.models.crm import Lead
from app.models.identity import Organization
from app.models.voice import Agent
from app.schemas.campaigns import RetryPolicy
from app.services import calls
from app.services import campaigns as service
from app.services.calls import CallNotAllowedError, CallRequest

log = logging.getLogger(__name__)

DISPATCH_BATCH = 50
CAPACITY_BACKOFF_SECONDS = 60
SIMULATED_DELAY_SECONDS = (5, 20)
# A lead stuck in `calling` this long with a non-terminal call is given up on; the
# call record itself is left to the call core's reconciliation.
STALE_CALLING_HOURS = 6


# ---------------------------------------------------------------- jobs


async def dispatch_campaigns(ctx: dict[str, Any]) -> dict[str, int]:
    """Cron: place due calls for every running campaign. One transaction per campaign."""
    now = service.utcnow()
    sessions = get_sessionmaker()
    async with sessions() as db:
        ids = (
            await db.execute(
                select(Campaign.id)
                .where(
                    Campaign.status.in_(ACTIVE_STATUSES),
                    or_(Campaign.backoff_until.is_(None), Campaign.backoff_until <= now),
                )
                .order_by(Campaign.created_at)
            )
        ).scalars()
        campaign_ids = list(ids.all())
    totals: dict[str, int] = {"campaigns": 0, "dispatched": 0, "skipped": 0, "deferred": 0}
    for campaign_id in campaign_ids:
        try:
            async with sessions() as db, db.begin():
                result = await _dispatch_one(db, ctx, campaign_id)
        except Exception:
            log.exception("campaign_dispatch_failed", extra={"campaign_id": str(campaign_id)})
            continue
        if result:
            totals["campaigns"] += 1
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + value
    return totals


async def advance_campaign_leads(ctx: dict[str, Any]) -> dict[str, int]:
    """Cron: reconcile finished calls onto campaign leads and close finished campaigns."""
    sessions = get_sessionmaker()
    async with sessions() as db:
        running = (
            await db.execute(select(Campaign.id).where(Campaign.status == CampaignStatus.RUNNING))
        ).scalars()
        in_flight = (
            await db.execute(
                select(CampaignLead.campaign_id)
                .where(CampaignLead.status == CampaignLeadStatus.CALLING)
                .distinct()
            )
        ).scalars()
        campaign_ids = list(dict.fromkeys([*running.all(), *in_flight.all()]))
    totals: dict[str, int] = {"campaigns": 0, "reconciled": 0, "completed": 0}
    for campaign_id in campaign_ids:
        try:
            async with sessions() as db, db.begin():
                result = await _advance_one(db, campaign_id)
        except Exception:
            log.exception("campaign_advance_failed", extra={"campaign_id": str(campaign_id)})
            continue
        if result:
            totals["campaigns"] += 1
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + value
    return totals


FUNCTIONS: list[Any] = []
CRON_JOBS = [
    cron(dispatch_campaigns, second={0, 30}, run_at_startup=False),
    cron(advance_campaign_leads, second={15, 45}, run_at_startup=False),
]


# ---------------------------------------------------------------- dispatch


async def _lock_campaign(db: AsyncSession, campaign_id: uuid.UUID) -> Campaign | None:
    """Row lock so only one worker works on a campaign at a time; others skip it."""
    return (
        await db.execute(
            select(Campaign).where(Campaign.id == campaign_id).with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()


async def _capacity(db: AsyncSession, campaign: Campaign, now: datetime) -> int:
    """Calls that may start now: concurrency, per-minute rate and the usage limit."""
    active = await _count_calls(
        db, campaign, Call.state.in_((CallState.QUEUED, CallState.DIALING, CallState.IN_PROGRESS))
    )
    recent = await _count_calls(db, campaign, Call.created_at >= now - timedelta(seconds=60))
    capacity = min(
        campaign.max_concurrency - active, campaign.calls_per_minute - recent, DISPATCH_BATCH
    )
    if campaign.max_calls is not None:
        remaining = campaign.max_calls - await _count_calls(db, campaign)
        if remaining <= 0:
            await db.execute(
                update(CampaignLead)
                .where(
                    CampaignLead.campaign_id == campaign.id,
                    CampaignLead.status == CampaignLeadStatus.PENDING,
                )
                .values(status=CampaignLeadStatus.SKIPPED, skip_reason=service.SKIP_USAGE_LIMIT)
            )
            return 0
        capacity = min(capacity, remaining)
    return max(capacity, 0)


async def _count_calls(db: AsyncSession, campaign: Campaign, *conditions: Any) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Call)
            .where(Call.campaign_id == campaign.id, *conditions)
        )
        or 0
    )


async def _dispatch_one(
    db: AsyncSession, ctx: dict[str, Any], campaign_id: uuid.UUID
) -> dict[str, int]:
    campaign = await _lock_campaign(db, campaign_id)
    if campaign is None:
        return {}
    now = service.utcnow()
    if campaign.status == CampaignStatus.SCHEDULED:
        if campaign.start_at and campaign.start_at > now:
            return {}
        await service.activate(db, campaign, now, actor=None)
    if campaign.status != CampaignStatus.RUNNING:
        return {}
    if campaign.backoff_until and campaign.backoff_until > now:
        return {}
    if campaign.end_at and campaign.end_at <= now:
        return {}  # the reconciler closes it once in-flight calls finish

    agent = await db.get(Agent, campaign.agent_id)
    if not service.agent_can_call(agent):
        await service.pause_for_error(db, campaign, "The agent is no longer published and active")
        return {"paused": 1}
    assert agent is not None
    live = campaign.mode == CampaignMode.LIVE
    if live and not get_settings().live_calls_enabled:
        await service.pause_for_error(db, campaign, "Live calls are disabled on this deployment")
        return {"paused": 1}
    org = await db.get(Organization, campaign.organization_id)
    org_tz = org.timezone if org else None

    capacity = await _capacity(db, campaign, now)
    if capacity <= 0:
        return {"deferred": 0}
    rows = list(
        (
            await db.execute(
                select(CampaignLead)
                .where(
                    CampaignLead.campaign_id == campaign.id,
                    CampaignLead.status == CampaignLeadStatus.PENDING,
                    or_(
                        CampaignLead.next_attempt_at.is_(None), CampaignLead.next_attempt_at <= now
                    ),
                )
                .order_by(
                    CampaignLead.next_attempt_at.asc().nulls_first(),
                    CampaignLead.created_at,
                    CampaignLead.id,
                )
                .limit(capacity)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {"dispatched": 0}
    leads = {
        lead.id: lead
        for lead in (
            await db.execute(select(Lead).where(Lead.id.in_([r.lead_id for r in rows])))
        ).scalars()
    }
    result = {"dispatched": 0, "skipped": 0, "deferred": 0}
    for item in rows:
        lead = leads.get(item.lead_id)
        if lead is None:
            item.status = CampaignLeadStatus.SKIPPED
            item.skip_reason = service.SKIP_ARCHIVED
            result["skipped"] += 1
            continue
        tz = service.resolve_zone(lead.timezone, campaign.timezone, org_tz)
        if not service.within_calling_hours(campaign.calling_hours, now, tz):
            # Park the lead until its own next window so it stops consuming batch slots.
            item.next_attempt_at = service.next_calling_window(
                campaign.calling_hours, now, tz
            ) or now + timedelta(hours=1)
            result["deferred"] += 1
            continue
        request = CallRequest(
            organization_id=campaign.organization_id,
            agent=agent,
            lead=lead,
            campaign_id=campaign.id,
            initiated_by_user_id=campaign.started_by_user_id,
            telephony_configuration_id=campaign.telephony_configuration_id,
            from_phone_number_id=campaign.from_phone_number_id,
            extra_context={"atlas_campaign_id": str(campaign.id)},
        )
        try:
            # Savepoint: a refused or failed placement leaves no half-written call row.
            async with db.begin_nested():
                call = await calls.start_outbound_call(db, request, live=live)
        except CallNotAllowedError as exc:
            item.status = CampaignLeadStatus.SKIPPED
            item.skip_reason = service.skip_reason_for(lead) or exc.message[:200]
            result["skipped"] += 1
            continue
        except PermissionDeniedError as exc:
            await service.pause_for_error(db, campaign, exc.message)
            result["paused"] = 1
            break
        except (RuntimeCapacityError, IntegrationUnavailableError) as exc:
            campaign.backoff_until = now + timedelta(seconds=CAPACITY_BACKOFF_SECONDS)
            log.warning(
                "campaign_backoff",
                extra={"campaign_id": str(campaign.id), "reason": type(exc).__name__},
            )
            result["backoff"] = 1
            break
        item.status = CampaignLeadStatus.CALLING
        item.attempts += 1
        item.last_call_id = call.id
        item.next_attempt_at = None
        result["dispatched"] += 1
        if call.mode == CallMode.SIMULATED and call.state == CallState.QUEUED:
            # A short random delay makes progress observable; if the queue is down the
            # call core's reconciliation completes the call instead.
            delay = random.randint(*SIMULATED_DELAY_SECONDS)  # noqa: S311 - not security
            await calls.schedule_simulated_completion(call.id, delay)
    campaign.stats = await service.compute_stats(db, campaign)
    return result


# ---------------------------------------------------------------- advance


def _apply_result(item: CampaignLead, call: Call, policy: RetryPolicy, now: datetime) -> None:
    item.last_outcome = call.outcome or call.state.value
    if call.state == CallState.COMPLETED:
        item.status = CampaignLeadStatus.COMPLETED
        return
    trigger = call.state.value
    if trigger in policy.retry_on:
        if item.attempts < policy.max_attempts:
            item.status = CampaignLeadStatus.PENDING
            item.next_attempt_at = now + timedelta(minutes=policy.backoff_minutes * item.attempts)
        else:
            item.status = CampaignLeadStatus.EXHAUSTED
        return
    item.status = CampaignLeadStatus.FAILED


async def _advance_one(db: AsyncSession, campaign_id: uuid.UUID) -> dict[str, int]:
    campaign = await _lock_campaign(db, campaign_id)
    if campaign is None:
        return {}
    now = service.utcnow()
    policy = RetryPolicy.model_validate(campaign.retry_policy)
    rows = (
        await db.execute(
            select(CampaignLead, Call)
            .join(Call, Call.id == CampaignLead.last_call_id, isouter=True)
            .where(
                CampaignLead.campaign_id == campaign.id,
                CampaignLead.status == CampaignLeadStatus.CALLING,
            )
            .with_for_update(skip_locked=True, of=CampaignLead)
        )
    ).all()
    result = {"reconciled": 0, "completed": 0}
    for item, call in rows:
        if call is None:
            # Claimed, but the call record is gone: give the lead its turn again.
            item.status = CampaignLeadStatus.PENDING
            item.next_attempt_at = None
            result["reconciled"] += 1
            continue
        if call.state not in TERMINAL_STATES:
            if item.updated_at <= now - timedelta(hours=STALE_CALLING_HOURS):
                item.status = CampaignLeadStatus.FAILED
                item.last_outcome = "stale"
                result["reconciled"] += 1
            continue
        _apply_result(item, call, policy, now)
        result["reconciled"] += 1

    if campaign.status == CampaignStatus.RUNNING:
        if campaign.end_at and campaign.end_at <= now:
            await db.execute(
                update(CampaignLead)
                .where(
                    CampaignLead.campaign_id == campaign.id,
                    CampaignLead.status == CampaignLeadStatus.PENDING,
                )
                .values(status=CampaignLeadStatus.SKIPPED, skip_reason=service.SKIP_WINDOW_ENDED)
            )
        open_items = await db.scalar(
            select(CampaignLead.id)
            .where(
                CampaignLead.campaign_id == campaign.id,
                CampaignLead.status.in_((CampaignLeadStatus.PENDING, CampaignLeadStatus.CALLING)),
            )
            .limit(1)
        )
        if open_items is None:
            await service.finish(db, campaign, now)
            result["completed"] = 1
            return result
    campaign.stats = await service.compute_stats(db, campaign)
    return result
