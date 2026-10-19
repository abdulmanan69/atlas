"""Call lifecycle core: create records, place calls (live or simulated), apply results.

This module is the single place that decides whether a real call may be placed.
Other services (campaigns, agents) call `start_outbound_call`; the runtime webhook
and reconciliation call `apply_runtime_result`. It also owns the tenant-facing read
side: history, detail, events, transcripts and recordings.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.config import get_settings
from app.core.exceptions import NotFoundError, PermissionDeniedError, ValidationFailedError
from app.core.permissions import Permission
from app.core.phone import InvalidPhoneError, normalize_phone
from app.db.pagination import page_scalars
from app.integrations.voice_runtime import artifacts
from app.integrations.voice_runtime.client import (
    RuntimeCapacityError,
    RuntimeRejectedError,
    RuntimeUnavailableError,
    WorkflowRun,
)
from app.integrations.voice_runtime.simulated import simulate
from app.models.calls import TERMINAL_STATES, Call, CallDirection, CallEvent, CallMode, CallState
from app.models.crm import Activity, ActivityType, ActorType, ConsentStatus, Lead, PipelineStage
from app.models.identity import Organization
from app.models.voice import Agent, AgentStatus
from app.services import audit, events, jobs
from app.services import latency as latency_parser
from app.services import leads as lead_service
from app.services.runtime_accounts import runtime_client

log = logging.getLogger(__name__)

OPT_OUT_FIELD = "opted_out"
INBOUND_LEAD_SOURCE = "inbound_call"
SIMULATED_COMPLETION_DELAY_SECONDS = 10
MAX_TRANSCRIPT_BYTES = 2 * 1024 * 1024
# Reconciliation: a live call still open this long after it started is re-checked with
# the runtime; one still open after GIVE_UP_AFTER with no result is closed as failed.
STALE_AFTER = timedelta(seconds=90)
GIVE_UP_AFTER = timedelta(hours=2)
SIMULATED_STUCK_AFTER = timedelta(minutes=2)
RECONCILE_BATCH = 200
_RECONCILE_LOCK_KEY = 0x41544C43  # one reconciliation pass at a time across workers

ResultSource = Literal["runtime", "simulation", "reconciliation"]
Artifact = Literal["transcript", "recording"]


@dataclass(frozen=True, slots=True)
class CallRequest:
    organization_id: uuid.UUID
    agent: Agent
    lead: Lead
    campaign_id: uuid.UUID | None = None
    initiated_by_user_id: uuid.UUID | None = None
    telephony_configuration_id: int | None = None
    from_phone_number_id: int | None = None
    extra_context: dict[str, str] | None = None


class CallNotAllowedError(ValidationFailedError):
    code = "call_not_allowed"


def lead_variables(lead: Lead) -> dict[str, str]:
    full = " ".join(p for p in (lead.first_name, lead.last_name) if p)
    values = {
        "first_name": lead.first_name,
        "last_name": lead.last_name or "",
        "full_name": full,
        "company": lead.company or "",
        "job_title": lead.job_title or "",
        "email": lead.email or "",
    }
    for key, value in (lead.custom_fields or {}).items():
        if isinstance(value, (str, int, float, bool)) and key not in values:
            values[str(key)] = str(value)
    return values


def check_callable(lead: Lead, agent: Agent) -> None:
    """Hard safety checks that apply to live and simulated calls alike."""
    if lead.deleted_at is not None:
        raise CallNotAllowedError("This lead is archived")
    if lead.do_not_call or lead.consent_status == ConsentStatus.REVOKED:
        raise CallNotAllowedError("This lead must not be called (do-not-call or consent revoked)")
    if not lead.phone_e164:
        raise CallNotAllowedError("This lead has no valid phone number")
    if agent.status != AgentStatus.ACTIVE or agent.published_version is None:
        raise CallNotAllowedError("The agent must be published and active")


# ------------------------------------------------------------------ events & state


def _event(db: AsyncSession, call: Call, type_: str, data: dict[str, Any] | None = None) -> None:
    db.add(
        CallEvent(
            organization_id=call.organization_id,
            call_id=call.id,
            type=type_,
            data=data or {},
            occurred_at=datetime.now(UTC),
        )
    )


def _set_state(
    db: AsyncSession, call: Call, state: CallState, *, reason: str | None = None
) -> bool:
    """Move the call to `state`, recording the transition. Returns False if unchanged."""
    if call.state == state:
        return False
    data: dict[str, Any] = {"from": call.state.value, "to": state.value}
    if reason:
        data["reason"] = reason
    call.state = state
    _event(db, call, "state_changed", data)
    return True


def _event_data(call: Call) -> dict[str, Any]:
    return {
        "call_id": str(call.id),
        "lead_id": str(call.lead_id) if call.lead_id else None,
        "agent_id": str(call.agent_id) if call.agent_id else None,
        "campaign_id": str(call.campaign_id) if call.campaign_id else None,
        "direction": call.direction.value,
        "mode": call.mode.value,
        "state": call.state.value,
    }


def _completed_data(call: Call) -> dict[str, Any]:
    return {
        **_event_data(call),
        "outcome": call.outcome,
        "duration_seconds": call.duration_seconds,
        "opted_out": call.opted_out,
        "captured": call.captured,
        "error_code": call.error_code,
        "ended_at": call.ended_at.isoformat() if call.ended_at else None,
    }


async def _fail(db: AsyncSession, call: Call, code: str, message: str) -> None:
    """Close a call that produced no result (never dialed, or nothing ever came back)."""
    call.error_code = code
    call.error_message = message[:500]
    call.ended_at = call.ended_at or datetime.now(UTC)
    if not _set_state(db, call, CallState.FAILED, reason=code):
        return
    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    await _record_on_lead(db, call, lead, actor=ActorType.SYSTEM)
    await events.emit(db, call.organization_id, "call.completed", _completed_data(call))


# ------------------------------------------------------------------ placing calls


async def start_outbound_call(db: AsyncSession, req: CallRequest, *, live: bool) -> Call:
    """Create the call record and place the call (live) or schedule a simulation.

    `live=True` is only honoured when ATLAS_LIVE_CALLS_ENABLED is true; otherwise
    the call is simulated and clearly marked. Callers must separately enforce the
    `campaigns:launch_live` permission before passing live=True.
    """
    settings = get_settings()
    check_callable(req.lead, req.agent)
    if live and not settings.live_calls_enabled:
        raise PermissionDeniedError(
            "Live calls are disabled on this deployment (ATLAS_LIVE_CALLS_ENABLED=false)"
        )
    mode = CallMode.LIVE if live else CallMode.SIMULATED
    call = Call(
        organization_id=req.organization_id,
        lead_id=req.lead.id,
        agent_id=req.agent.id,
        agent_version=req.agent.published_version,
        campaign_id=req.campaign_id,
        direction=CallDirection.OUTBOUND,
        mode=mode,
        state=CallState.QUEUED,
        to_phone_e164=req.lead.phone_e164,
        initiated_by_user_id=req.initiated_by_user_id,
        runtime_workflow_id=req.agent.runtime_workflow_id,
    )
    db.add(call)
    await db.flush()
    _event(
        db,
        call,
        "created",
        {
            "direction": call.direction.value,
            "mode": call.mode.value,
            "campaign_id": str(req.campaign_id) if req.campaign_id else None,
        },
    )

    if mode == CallMode.SIMULATED:
        # Completed by the worker (app.workers.calls.complete_simulated_call) after a short
        # delay so progress is observable; nothing leaves the platform.
        return call

    if not req.agent.runtime_workflow_uuid or not req.lead.phone_e164:
        await _fail(db, call, "agent_not_synced", "The agent is not published to the voice service")
        return call
    context = {
        **lead_variables(req.lead),
        **(req.extra_context or {}),
        "atlas_call_id": str(call.id),
        "atlas_lead_id": str(req.lead.id),
    }
    try:
        async with runtime_client(db, req.organization_id) as client:
            triggered = await client.trigger_outbound_call(
                req.agent.runtime_workflow_uuid,
                phone_number=req.lead.phone_e164,
                context=context,
                telephony_configuration_id=req.telephony_configuration_id,
                from_phone_number_id=req.from_phone_number_id,
            )
    except RuntimeCapacityError:
        # Leave queued; the campaign scheduler retries with backoff.
        raise
    call.runtime_run_id = str(triggered.workflow_run_id)
    call.started_at = datetime.now(UTC)
    _set_state(db, call, CallState.DIALING)
    await events.emit(db, req.organization_id, "call.started", _event_data(call))
    return call


async def start_call(
    db: AsyncSession, ctx: TenantContext, *, lead_id: uuid.UUID, agent_id: uuid.UUID, live: bool
) -> Call:
    """One call started by a user from the UI/API (not part of a campaign)."""
    from app.services import agents as agent_service  # local import: avoids a cycle

    if live:
        ctx.require(Permission.CAMPAIGNS_LAUNCH_LIVE)
    lead = await lead_service.get_lead(db, ctx, lead_id)
    agent = await agent_service.get_agent(db, ctx, agent_id)
    call = await start_outbound_call(
        db,
        CallRequest(
            organization_id=ctx.org_id, agent=agent, lead=lead, initiated_by_user_id=ctx.user_id
        ),
        live=live,
    )
    await audit.record(
        db,
        action="call.started",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="call",
        resource_id=call.id,
        changes={
            "lead_id": str(lead.id),
            "agent_id": str(agent.id),
            "mode": call.mode.value,
            "live_requested": live,
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(call)
    if call.mode == CallMode.SIMULATED and call.state == CallState.QUEUED:
        await schedule_simulated_completion(call.id)
    return call


async def schedule_simulated_completion(
    call_id: uuid.UUID, delay_seconds: int = SIMULATED_COMPLETION_DELAY_SECONDS
) -> bool:
    """Ask the worker to finish a simulated call after `delay_seconds`.

    Returns False when the queue is unavailable; reconciliation then completes the
    call on its next pass, so nothing is lost.
    """
    try:
        return await jobs.enqueue(
            "complete_simulated_call",
            str(call_id),
            job_id=f"simulated-call:{call_id}",
            defer_by=delay_seconds,
        )
    except jobs.QueueUnavailableError:
        log.warning("simulated_completion_not_scheduled", extra={"call_id": str(call_id)})
        return False


# ------------------------------------------------------------------ results


async def complete_simulated_call(db: AsyncSession, call_id: uuid.UUID) -> Call | None:
    """Produce the synthetic result for a simulated call. Idempotent."""
    call = await db.get(Call, call_id)
    if call is None or call.mode != CallMode.SIMULATED or call.state in TERMINAL_STATES:
        return call
    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    agent = await db.get(Agent, call.agent_id) if call.agent_id else None
    outcomes: list[str] = []
    if agent is not None:
        from app.services.agents import config_of  # local import: avoids a cycle

        outcomes = [o.code for o in (await config_of(db, agent)).outcomes]
    result = simulate(
        seed=str(call.id),
        first_name=lead.first_name if lead else "there",
        available_outcomes=outcomes,
    )
    now = datetime.now(UTC)
    call.started_at = call.started_at or now - timedelta(seconds=result.duration_seconds)
    call.ended_at = now
    call.duration_seconds = result.duration_seconds
    call.outcome = result.outcome
    call.captured = result.captured
    call.transcript = result.transcript or None
    call.opted_out = result.opted_out
    call.latency = (
        latency_parser.simulated(
            str(call.id), duration_seconds=result.duration_seconds, started_at=call.started_at
        )
        if result.answered
        else None
    )
    call.result_synced_at = now
    await events.emit(
        db,
        call.organization_id,
        "call.started",
        {**_event_data(call), "state": CallState.IN_PROGRESS.value},
    )
    _set_state(
        db,
        call,
        CallState.COMPLETED if result.answered else CallState.NO_ANSWER,
        reason="simulated",
    )
    _event(
        db,
        call,
        "result_applied",
        {
            "source": "simulation",
            "outcome": call.outcome,
            "opted_out": call.opted_out,
            "duration_seconds": call.duration_seconds,
        },
    )
    await _record_on_lead(db, call, lead, actor=ActorType.SYSTEM)
    await events.emit(db, call.organization_id, "call.completed", _completed_data(call))
    return call


def _summarize_runtime_result(run: dict[str, Any]) -> dict[str, Any]:
    """Keep only non-sensitive, bounded fields from a runtime run payload."""
    gathered = run.get("gathered_context") or {}
    usage = run.get("usage_info") or {}
    return {
        "call_duration_seconds": usage.get("call_duration_seconds"),
        "disposition": gathered.get("call_disposition"),
        "call_status": gathered.get("call_status"),
    }


def _telephony_error(status: str, gathered: dict[str, Any]) -> str:
    parts = [f"Telephony status: {status}"]
    if gathered.get("telephony_error_code"):
        parts.append(f"provider code {gathered['telephony_error_code']}")
    if gathered.get("telephony_sip_code"):
        parts.append(f"SIP {gathered['telephony_sip_code']}")
    return ", ".join(parts)


async def apply_runtime_result(
    db: AsyncSession,
    call: Call,
    run: dict[str, Any],
    *,
    actor: ActorType = ActorType.AI_AGENT,
    source: ResultSource = "runtime",
) -> Call:
    """Map a runtime run (verified by re-fetch) onto the call record. Idempotent.

    A result already on record is never overwritten: duplicate deliveries and
    reconciliation passes after a webhook are no-ops. A call closed earlier without a
    result (e.g. `no_result`) is updated when the real result finally arrives.
    """
    if call.result_synced_at is not None and call.state in TERMINAL_STATES:
        return call
    gathered: dict[str, Any] = run.get("gathered_context") or {}
    usage: dict[str, Any] = run.get("usage_info") or {}
    cost: dict[str, Any] = run.get("cost_info") or {}
    now = datetime.now(UTC)
    if not run.get("is_completed"):
        if call.state in (CallState.QUEUED, CallState.DIALING):
            call.started_at = call.started_at or now
            _set_state(db, call, CallState.IN_PROGRESS, reason=source)
            await events.emit(db, call.organization_id, "call.started", _event_data(call))
        return call

    duration = usage.get("call_duration_seconds") or cost.get("call_duration_seconds")
    call.duration_seconds = int(duration) if isinstance(duration, (int, float)) else None
    call.ended_at = call.ended_at or now
    if call.started_at is None:
        call.started_at = call.ended_at - timedelta(seconds=call.duration_seconds or 0)
    # Telephony statuses verified in the runtime's enums (TelephonyCallStatus).
    status = str(gathered.get("call_status") or "").lower()
    if status in ("no_answer", "no-answer", "unanswered"):
        state = CallState.NO_ANSWER
    elif status == "busy":
        state = CallState.BUSY
    elif status in ("failed", "error"):
        state = CallState.FAILED
        call.error_code = "telephony_failed"
        call.error_message = _telephony_error(status, gathered)[:500]
    elif status in ("canceled", "cancelled"):
        state = CallState.CANCELED
    else:
        state = CallState.COMPLETED
        call.error_code = None
        call.error_message = None
    call.outcome = gathered.get("call_disposition") or gathered.get("mapped_call_disposition")
    extracted = gathered.get("extracted_variables")
    captured = {k: v for k, v in (extracted or {}).items() if k != OPT_OUT_FIELD}
    call.captured = captured
    call.opted_out = bool((extracted or {}).get(OPT_OUT_FIELD)) or call.outcome == "do_not_call"
    call.usage = {
        k: v for k, v in usage.items() if k in ("llm", "stt", "tts", "call_duration_seconds")
    }
    call.runtime_transcript_key = run.get("transcript_url")
    call.runtime_recording_key = run.get("recording_url")
    call.recording_available = bool(run.get("recording_url"))
    # Per-turn response times from the saved event log (ADR 0006); never fails the sync.
    call.latency = latency_parser.from_run_logs(run.get("logs"))
    call.result_synced_at = now
    _set_state(db, call, state, reason=source)
    _event(
        db,
        call,
        "result_applied",
        {
            "source": source,
            "outcome": call.outcome,
            "opted_out": call.opted_out,
            "duration_seconds": call.duration_seconds,
            "recording_available": call.recording_available,
            "transcript_available": bool(call.runtime_transcript_key),
            "latency_measured": call.latency is not None,
        },
    )
    lead = await db.get(Lead, call.lead_id) if call.lead_id else None
    await _record_on_lead(db, call, lead, actor=actor)
    await events.emit(db, call.organization_id, "call.completed", _completed_data(call))
    return call


async def _record_on_lead(
    db: AsyncSession, call: Call, lead: Lead | None, *, actor: ActorType
) -> None:
    """Timeline entry, last-contacted, and the opt-out guarantee."""
    if lead is None:
        return
    if call.state == CallState.COMPLETED:
        lead.last_contacted_at = call.ended_at
    if call.opted_out:
        lead.do_not_call = True
        lead.consent_status = ConsentStatus.REVOKED
    label = {
        CallState.COMPLETED: f"Call completed · {call.outcome or 'no outcome'}",
        CallState.NO_ANSWER: "Call not answered",
        CallState.BUSY: "Line busy",
        CallState.FAILED: "Call failed",
        CallState.CANCELED: "Call canceled",
    }.get(call.state, "Call updated")
    if call.direction == CallDirection.INBOUND:
        label = f"Inbound · {label}"
    if call.mode == CallMode.SIMULATED:
        label = f"[Simulated] {label}"
    db.add(
        Activity(
            organization_id=call.organization_id,
            lead_id=lead.id,
            type=ActivityType.CALL,
            actor_type=actor,
            summary=label[:500],
            data={
                "call_id": str(call.id),
                "mode": call.mode.value,
                "direction": call.direction.value,
                "outcome": call.outcome,
                "duration_seconds": call.duration_seconds,
                "captured": call.captured,
                "opted_out": call.opted_out,
            },
            occurred_at=call.ended_at or datetime.now(UTC),
        )
    )


async def find_by_runtime_run(
    db: AsyncSession, runtime_run_id: str, *, organization_id: uuid.UUID | None = None
) -> Call | None:
    stmt = select(Call).where(Call.runtime_run_id == runtime_run_id)
    if organization_id is not None:
        stmt = stmt.where(Call.organization_id == organization_id)
    return (await db.execute(stmt)).scalar_one_or_none()


# ------------------------------------------------------------------ runtime sync


async def process_runtime_run(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    call_id: uuid.UUID | None,
    workflow_id: str,
    run_id: str,
    source: ResultSource = "runtime",
) -> Call | None:
    """Fetch a run from the runtime (the authoritative source) and apply it to its call.

    Inbound calls the platform has not seen yet get a call record (and a lead) here.
    Returns None when the run is not a call ATLAS tracks (e.g. a browser test session).
    """
    async with runtime_client(db, organization_id) as client:
        run = await client.get_run(workflow_id, run_id)
    call: Call | None = None
    if call_id is not None:
        call = (
            await db.execute(
                select(Call).where(Call.id == call_id, Call.organization_id == organization_id)
            )
        ).scalar_one_or_none()
    if call is None:
        call = await find_by_runtime_run(db, str(run.id), organization_id=organization_id)
    if call is None:
        call = await _call_for_unknown_run(db, organization_id, workflow_id, run)
        if call is None:
            return None
    if call.runtime_run_id is None:
        call.runtime_run_id = str(run.id)
    if call.runtime_workflow_id is None:
        call.runtime_workflow_id = str(run.workflow_id)
    await apply_runtime_result(db, call, run.model_dump(), source=source)
    await db.flush()
    return call


async def _call_for_unknown_run(
    db: AsyncSession, organization_id: uuid.UUID, workflow_id: str, run: WorkflowRun
) -> Call | None:
    initial = run.initial_context or {}
    if str(initial.get("atlas_test", "")).lower() == "true":
        return None  # browser test session from the agent editor: not a call record
    # Verified: inbound telephony stamps direction="inbound" on initial_context and
    # call_type on the run (routes/telephony.py, `_create_inbound_workflow_run`).
    if run.call_type != "inbound" and initial.get("direction") != "inbound":
        log.info("runtime_run_ignored", extra={"reason": "not an inbound call"})
        return None
    agent = (
        await db.execute(
            select(Agent).where(
                Agent.organization_id == organization_id,
                Agent.runtime_workflow_id == str(workflow_id),
            )
        )
    ).scalar_one_or_none()
    if agent is None:
        raise NotFoundError("Agent not found")
    return await ensure_inbound_call(db, organization_id, agent, run)


def _normalize_or_none(raw: Any, region: str) -> str | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return normalize_phone(raw, region)
    except InvalidPhoneError:
        return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def ensure_inbound_call(
    db: AsyncSession, organization_id: uuid.UUID, agent: Agent, run: WorkflowRun
) -> Call:
    """Create the call record for an inbound run, matching or creating the lead by caller."""
    org = await db.get(Organization, organization_id)
    region = org.default_country if org else "US"
    initial = run.initial_context or {}
    caller = _normalize_or_none(initial.get("caller_number"), region)
    called = _normalize_or_none(initial.get("called_number"), region)
    lead: Lead | None = None
    created = False
    if caller:
        lead, created = await _match_or_create_lead(
            db, organization_id, caller, raw=str(initial.get("caller_number"))
        )
    call = Call(
        organization_id=organization_id,
        lead_id=lead.id if lead else None,
        agent_id=agent.id,
        agent_version=agent.published_version,
        direction=CallDirection.INBOUND,
        mode=CallMode.LIVE,
        state=CallState.IN_PROGRESS,
        to_phone_e164=called,
        from_phone_e164=caller,
        started_at=_parse_timestamp(run.created_at) or datetime.now(UTC),
        runtime_workflow_id=str(run.workflow_id),
        runtime_run_id=str(run.id),
    )
    db.add(call)
    await db.flush()
    _event(db, call, "created", {"direction": "inbound", "mode": "live"})
    if lead is not None:
        _event(db, call, "lead_matched", {"lead_id": str(lead.id), "created": created})
    else:
        _event(db, call, "lead_unmatched", {"reason": "caller number unavailable or invalid"})
    await events.emit(db, organization_id, "call.started", _event_data(call))
    return call


async def _match_or_create_lead(
    db: AsyncSession, organization_id: uuid.UUID, phone: str, *, raw: str
) -> tuple[Lead | None, bool]:
    lead = (
        await db.execute(
            select(Lead).where(
                Lead.organization_id == organization_id,
                Lead.phone_e164 == phone,
                Lead.deleted_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if lead is not None:
        return lead, False
    stage = (
        (
            await db.execute(
                select(PipelineStage)
                .where(PipelineStage.organization_id == organization_id)
                .order_by(PipelineStage.is_default.desc(), PipelineStage.position)
            )
        )
        .scalars()
        .first()
    )
    if stage is None:
        log.warning("inbound_lead_not_created", extra={"reason": "no pipeline stage"})
        return None, False
    lead = Lead(
        organization_id=organization_id,
        first_name="Inbound caller",
        phone_e164=phone,
        phone_raw=raw[:64],
        source=INBOUND_LEAD_SOURCE,
        stage_id=stage.id,
        tags=["inbound"],
    )
    db.add(lead)
    await db.flush()
    db.add(
        Activity(
            organization_id=organization_id,
            lead_id=lead.id,
            type=ActivityType.SYSTEM,
            actor_type=ActorType.SYSTEM,
            summary="Lead created from an inbound call",
            data={"source": INBOUND_LEAD_SOURCE},
        )
    )
    await events.emit(
        db,
        organization_id,
        "lead.created",
        {"lead_id": str(lead.id), "source": INBOUND_LEAD_SOURCE},
    )
    return lead, True


async def sync_from_runtime(
    db: AsyncSession, call: Call, *, source: ResultSource = "reconciliation"
) -> Call:
    """Re-fetch the call's run and apply it. No-op for calls without runtime ids."""
    if not (call.runtime_run_id and call.runtime_workflow_id):
        return call
    async with runtime_client(db, call.organization_id) as client:
        run = await client.get_run(call.runtime_workflow_id, call.runtime_run_id)
    return await apply_runtime_result(db, call, run.model_dump(), source=source)


async def reconcile_open_calls(db: AsyncSession, *, now: datetime | None = None) -> dict[str, int]:
    """Catch up calls whose completion never reached us (lost webhook, runtime outage).

    Live calls open longer than STALE_AFTER are re-fetched; those still open after
    GIVE_UP_AFTER with no result are closed as failed (`no_result`). Simulated calls
    whose completion job was lost are completed here. Runs inside one transaction with
    a per-call savepoint, so one bad record cannot block the rest.
    """
    now = now or datetime.now(UTC)
    counts = {
        "checked": 0,
        "updated": 0,
        "unreachable": 0,
        "errors": 0,
        "simulated_completed": 0,
    }
    locked = await db.scalar(
        text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": _RECONCILE_LOCK_KEY}
    )
    if not locked:
        return {**counts, "skipped": 1}

    stale = (
        (
            await db.execute(
                select(Call)
                .where(
                    Call.mode == CallMode.LIVE,
                    Call.state.in_((CallState.DIALING, CallState.IN_PROGRESS)),
                    func.coalesce(Call.started_at, Call.created_at) < now - STALE_AFTER,
                )
                .order_by(Call.created_at)
                .limit(RECONCILE_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for call in stale:
        counts["checked"] += 1
        opened_at = call.started_at or call.created_at
        before = (call.state, call.result_synced_at)
        try:
            async with db.begin_nested():
                try:
                    await sync_from_runtime(db, call)
                except RuntimeUnavailableError:
                    counts["unreachable"] += 1
                except RuntimeRejectedError as exc:
                    if exc.runtime_status == 404:
                        await _fail(
                            db, call, "no_result", "The voice service has no record of this call"
                        )
                    else:
                        log.warning(
                            "call_reconcile_rejected",
                            extra={"call_id": str(call.id), "status": exc.runtime_status},
                        )
                if call.state not in TERMINAL_STATES and opened_at < now - GIVE_UP_AFTER:
                    await _fail(
                        db,
                        call,
                        "no_result",
                        "No result was received from the voice service within 2 hours",
                    )
        except Exception:
            counts["errors"] += 1
            log.exception("call_reconcile_failed", extra={"call_id": str(call.id)})
            continue
        if (call.state, call.result_synced_at) != before:
            counts["updated"] += 1

    stuck = (
        (
            await db.execute(
                select(Call.id)
                .where(
                    Call.mode == CallMode.SIMULATED,
                    Call.state == CallState.QUEUED,
                    Call.created_at < now - SIMULATED_STUCK_AFTER,
                )
                .order_by(Call.created_at)
                .limit(RECONCILE_BATCH)
            )
        )
        .scalars()
        .all()
    )
    for call_id in stuck:
        try:
            async with db.begin_nested():
                await complete_simulated_call(db, call_id)
        except Exception:
            counts["errors"] += 1
            log.exception("simulated_call_complete_failed", extra={"call_id": str(call_id)})
            continue
        counts["simulated_completed"] += 1
    return counts


# ------------------------------------------------------------------ read side


@dataclass(slots=True)
class CallFilters:
    created_from: datetime | None = None
    created_to: datetime | None = None
    agent_id: uuid.UUID | None = None
    campaign_id: uuid.UUID | None = None
    lead_id: uuid.UUID | None = None
    state: CallState | None = None
    outcome: str | None = None
    mode: CallMode | None = None
    direction: CallDirection | None = None


def _scoped(org_id: uuid.UUID) -> Select[tuple[Call]]:
    return select(Call).where(Call.organization_id == org_id)


def _apply_filters(stmt: Select[tuple[Call]], f: CallFilters) -> Select[tuple[Call]]:
    if f.created_from:
        stmt = stmt.where(Call.created_at >= f.created_from)
    if f.created_to:
        stmt = stmt.where(Call.created_at < f.created_to)
    if f.agent_id:
        stmt = stmt.where(Call.agent_id == f.agent_id)
    if f.campaign_id:
        stmt = stmt.where(Call.campaign_id == f.campaign_id)
    if f.lead_id:
        stmt = stmt.where(Call.lead_id == f.lead_id)
    if f.state:
        stmt = stmt.where(Call.state == f.state)
    if f.outcome:
        stmt = stmt.where(Call.outcome == f.outcome)
    if f.mode:
        stmt = stmt.where(Call.mode == f.mode)
    if f.direction:
        stmt = stmt.where(Call.direction == f.direction)
    return stmt


async def list_calls(
    db: AsyncSession, ctx: TenantContext, f: CallFilters, page: int, page_size: int
) -> tuple[list[Call], int]:
    base = _apply_filters(_scoped(ctx.org_id), f)
    return await page_scalars(db, base.order_by(Call.created_at.desc(), Call.id), page, page_size)


async def list_lead_calls(
    db: AsyncSession, ctx: TenantContext, lead_id: uuid.UUID, page: int, page_size: int
) -> tuple[list[Call], int]:
    # Archived leads keep their call history, so no deleted_at filter here.
    exists = await db.scalar(
        select(Lead.id).where(Lead.id == lead_id, Lead.organization_id == ctx.org_id)
    )
    if not exists:
        raise NotFoundError("Lead not found")
    return await list_calls(db, ctx, CallFilters(lead_id=lead_id), page, page_size)


async def get_call(db: AsyncSession, ctx: TenantContext, call_id: uuid.UUID) -> Call:
    call = (await db.execute(_scoped(ctx.org_id).where(Call.id == call_id))).scalar_one_or_none()
    if call is None:
        raise NotFoundError("Call not found")
    return call


async def get_latency(db: AsyncSession, ctx: TenantContext, call_id: uuid.UUID) -> dict[str, Any]:
    """The stored per-turn latency of a call (tenant-scoped); 404 when not measured."""
    call = await get_call(db, ctx, call_id)
    if not isinstance(call.latency, dict) or not isinstance(call.latency.get("summary"), dict):
        raise NotFoundError("No response times were measured for this call")
    return call.latency


async def list_events(db: AsyncSession, ctx: TenantContext, call_id: uuid.UUID) -> list[CallEvent]:
    call = await get_call(db, ctx, call_id)
    rows = await db.execute(
        select(CallEvent)
        .where(CallEvent.call_id == call.id, CallEvent.organization_id == ctx.org_id)
        .order_by(CallEvent.seq)
    )
    return list(rows.scalars().all())


def has_transcript(call: Call) -> bool:
    if call.transcript:
        return True
    return call.mode != CallMode.SIMULATED and bool(call.runtime_transcript_key)


async def _artifact_location(db: AsyncSession, call: Call, artifact: Artifact) -> str | None:
    if not (call.runtime_run_id and call.runtime_workflow_id):
        return None
    async with runtime_client(db, call.organization_id) as client:
        run = await client.get_run(call.runtime_workflow_id, call.runtime_run_id)
        if not run.public_access_token:
            return None
        return await client.artifact_location(run.public_access_token, artifact)


async def get_transcript(
    db: AsyncSession, ctx: TenantContext, call_id: uuid.UUID
) -> tuple[Call, str]:
    """The conversation text. Fetched from the runtime once, then stored on the call."""
    call = await get_call(db, ctx, call_id)
    if call.transcript:
        return call, call.transcript
    missing = NotFoundError("No transcript is available for this call")
    if not has_transcript(call):
        raise missing
    location = await _artifact_location(db, call, "transcript")
    if location is None:
        raise missing
    content, truncated = await artifacts.fetch_artifact(
        location,
        max_bytes=MAX_TRANSCRIPT_BYTES,
        request_timeout=get_settings().voice_runtime_timeout_seconds,
    )
    transcript = content.decode("utf-8", errors="replace")
    if not transcript.strip():
        raise missing
    if truncated:
        transcript += "\n[transcript truncated]"
    call.transcript = transcript
    _event(db, call, "transcript_cached", {"bytes": len(content), "truncated": truncated})
    await db.flush()
    return call, transcript


async def open_recording(
    db: AsyncSession, ctx: TenantContext, call_id: uuid.UUID
) -> tuple[Call, artifacts.ArtifactStream]:
    """Start streaming the recording. The access is audited before any audio is sent."""
    call = await get_call(db, ctx, call_id)
    missing = NotFoundError("No recording is available for this call")
    if not call.recording_available or not call.runtime_recording_key:
        raise missing
    location = await _artifact_location(db, call, "recording")
    if location is None:
        raise missing
    stream = await artifacts.open_artifact(
        location, request_timeout=get_settings().voice_runtime_timeout_seconds
    )
    await audit.record(
        db,
        action="recording.accessed",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="call",
        resource_id=call.id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.commit()
    return call, stream
