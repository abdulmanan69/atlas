"""Call jobs: authoritative result processing, simulated completions, reconciliation.

Every job opens its own session and is idempotent (ARQ may retry after a crash).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from arq import Retry, cron

from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.client import RuntimeUnavailableError
from app.models.webhooks import WebhookEvent, WebhookEventStatus
from app.services import calls as call_service

log = logging.getLogger(__name__)

MAX_TRIES = 5
_RETRY_BASE_SECONDS = 30
_RETRY_MAX_SECONDS = 300


async def _mark_event(
    event_id: uuid.UUID | None, status: WebhookEventStatus, error: str | None = None
) -> None:
    if event_id is None:
        return
    async with get_sessionmaker()() as db:
        event = await db.get(WebhookEvent, event_id)
        if event is None:
            return
        event.status = status
        event.error = error[:500] if error else None
        event.processed_at = datetime.now(UTC)
        await db.commit()


async def process_runtime_run(
    ctx: dict[str, Any],
    org_id: str,
    call_id: str | None,
    workflow_id: str,
    run_id: str,
    *,
    webhook_event_id: str | None = None,
) -> str:
    """Re-fetch a run from the voice service and apply it to the call record.

    Retries with backoff while the voice service is unreachable; after MAX_TRIES the
    delivery is marked failed and reconciliation takes over.
    """
    organization_id = uuid.UUID(org_id)
    call_uuid = uuid.UUID(call_id) if call_id else None
    event_id = uuid.UUID(webhook_event_id) if webhook_event_id else None
    job_try = int(ctx.get("job_try") or 1)
    async with get_sessionmaker()() as db:
        try:
            call = await call_service.process_runtime_run(
                db, organization_id, call_id=call_uuid, workflow_id=workflow_id, run_id=run_id
            )
        except RuntimeUnavailableError as exc:
            await db.rollback()
            if job_try < MAX_TRIES:
                delay = min(_RETRY_BASE_SECONDS * 2 ** (job_try - 1), _RETRY_MAX_SECONDS)
                raise Retry(defer=delay) from exc
            await _mark_event(event_id, WebhookEventStatus.FAILED, "voice service unavailable")
            raise
        except Exception as exc:
            await db.rollback()
            await _mark_event(event_id, WebhookEventStatus.FAILED, f"{type(exc).__name__}: {exc}")
            raise
        if event_id is not None:
            event = await db.get(WebhookEvent, event_id)
            if event is not None:
                event.status = WebhookEventStatus.PROCESSED
                event.processed_at = datetime.now(UTC)
                if call is not None and event.call_id is None:
                    event.call_id = call.id
        await db.commit()
    log.info(
        "runtime_run_processed",
        extra={"organization_id": org_id, "tracked": call is not None},
    )
    return "processed" if call is not None else "ignored"


async def complete_simulated_call(ctx: dict[str, Any], call_id: str) -> str:
    """Finish a simulated (dry-run) call with its synthetic result."""
    async with get_sessionmaker()() as db:
        call = await call_service.complete_simulated_call(db, uuid.UUID(call_id))
        await db.commit()
    return call.state.value if call is not None else "missing"


async def reconcile_calls(ctx: dict[str, Any]) -> dict[str, int]:
    """Scheduled safety net for lost completions (see calls.reconcile_open_calls)."""
    async with get_sessionmaker()() as db:
        counts = await call_service.reconcile_open_calls(db)
        await db.commit()
    if counts.get("checked") or counts.get("simulated_completed"):
        log.info("calls_reconciled", extra=counts)
    return counts


FUNCTIONS = [process_runtime_run, complete_simulated_call, reconcile_calls]
CRON_JOBS = [cron(reconcile_calls, minute=set(range(0, 60, 2)), second=15, run_at_startup=False)]
