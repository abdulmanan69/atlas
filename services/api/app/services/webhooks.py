"""Inbound call events from the voice runtime.

Per ADR 0003 the runtime signs nothing, so this handler checks a per-tenant shared
secret in constant time, deduplicates on the delivery id, and never trusts the
payload for results: a worker re-fetches the run from the runtime API. The
verification strategy is isolated in `_verify` so it can become HMAC later.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, NotFoundError, ValidationFailedError
from app.core.logging import redact
from app.core.security import constant_time_equals, get_secret_box
from app.models.calls import Call
from app.models.voice import Agent, RuntimeAccount
from app.models.webhooks import WebhookEvent, WebhookEventStatus
from app.services import jobs

log = logging.getLogger(__name__)

PROVIDER = "voice_runtime"
ReceiptStatus = Literal["accepted", "duplicate", "processed"]
_ID = re.compile(r"^\d{1,18}$")
# Keys that may carry personal data in a delivery body; stored masked.
_PERSONAL = re.compile(r"(phone|number|caller|callee|email|name)", re.IGNORECASE)
_MAX_STORED_KEYS = 40
_MAX_STORED_VALUE = 200


@dataclass(frozen=True, slots=True)
class Delivery:
    secret: str | None
    delivery_id: str | None
    run_id_header: str | None
    body: bytes
    payload: Any


@dataclass(frozen=True, slots=True)
class Receipt:
    status: ReceiptStatus
    event_id: uuid.UUID | None


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return uuid.UUID(value.strip())
    except ValueError:
        return None


def _id_or_none(value: Any) -> str | None:
    """Runtime ids are integers; templates render them as strings."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and _ID.match(value.strip()):
        return value.strip()
    return None


def _store_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Bounded, redacted copy of the delivery for support; never read back as truth."""
    safe: dict[str, Any] = {}
    for key, value in payload.items():
        name = str(key)[:64]
        if _PERSONAL.search(name):
            safe[name] = "[REDACTED]"
        elif isinstance(value, (dict, list)):
            safe[name] = "[omitted]"
        elif isinstance(value, str):
            safe[name] = value[:_MAX_STORED_VALUE]
        else:
            safe[name] = value
        if len(safe) >= _MAX_STORED_KEYS:
            break
    redacted: dict[str, Any] = redact(safe)
    return redacted


async def _verify(db: AsyncSession, organization_id: uuid.UUID, provided: str | None) -> None:
    """Constant-time check of the shared secret configured for this tenant's runtime account.

    Always performs the comparison, even when no secret is configured, so timing does
    not reveal which tenants have voice set up.
    """
    account = (
        await db.execute(
            select(RuntimeAccount).where(RuntimeAccount.organization_id == organization_id)
        )
    ).scalar_one_or_none()
    expected = ""
    if account is not None and account.webhook_secret_encrypted:
        expected = get_secret_box().decrypt(account.webhook_secret_encrypted)
    matches = constant_time_equals(provided or "", expected)
    if not expected or not matches:
        log.warning(
            "webhook_rejected", extra={"organization_id": str(organization_id), "reason": "secret"}
        )
        raise AuthenticationError("Invalid webhook credentials")


async def receive_runtime_event(db: AsyncSession, delivery: Delivery) -> Receipt:
    """Accept one delivery: resolve tenant, verify, dedupe, queue the authoritative fetch."""
    if not isinstance(delivery.payload, dict):
        raise ValidationFailedError("Unsupported payload")
    payload: dict[str, Any] = delivery.payload
    call_id = _uuid_or_none(payload.get("atlas_call_id"))
    run_id = _id_or_none(payload.get("workflow_run_id")) or _id_or_none(delivery.run_id_header)
    workflow_id = _id_or_none(payload.get("workflow_id"))

    # Tenant comes from our own records, never from the body alone.
    call: Call | None = None
    organization_id: uuid.UUID | None = None
    if call_id is not None:
        call = await db.get(Call, call_id)
        if call is not None:
            organization_id = call.organization_id
            # The call's own runtime ids win over anything the body claims.
            workflow_id = call.runtime_workflow_id or workflow_id
            run_id = call.runtime_run_id or run_id
    if organization_id is None and workflow_id is not None:
        organization_id = await db.scalar(
            select(Agent.organization_id).where(Agent.runtime_workflow_id == workflow_id).limit(1)
        )
    if organization_id is None:
        raise NotFoundError("Not found")

    await _verify(db, organization_id, delivery.secret)
    if not run_id or not workflow_id:
        raise ValidationFailedError("Missing run identifiers")

    delivery_id = delivery.delivery_id or "sha256:" + hashlib.sha256(delivery.body).hexdigest()
    event = WebhookEvent(
        organization_id=organization_id,
        provider=PROVIDER,
        delivery_id=delivery_id[:128],
        payload=_store_payload(payload),
        status=WebhookEventStatus.RECEIVED,
        call_id=call.id if call else None,
    )
    try:
        async with db.begin_nested():
            db.add(event)
            await db.flush()
    except IntegrityError:
        log.info("webhook_duplicate_ignored", extra={"organization_id": str(organization_id)})
        return Receipt("duplicate", None)
    # Durable before anything asynchronous happens: a retry after this point is a duplicate.
    await db.commit()

    job_args = (str(organization_id), str(call.id) if call else None, workflow_id, run_id)
    try:
        await jobs.enqueue(
            "process_runtime_run",
            *job_args,
            job_id=f"runtime-event:{event.id}",
            webhook_event_id=str(event.id),
        )
        return Receipt("accepted", event.id)
    except jobs.QueueUnavailableError:
        pass

    # Degraded mode: the queue is down, so process now with a bound. Anything that
    # still slips through is recovered by reconciliation.
    from app.workers.calls import MAX_TRIES, process_runtime_run  # local: avoids a cycle

    timeout = get_settings().voice_runtime_timeout_seconds + 5
    try:
        await asyncio.wait_for(
            process_runtime_run({"job_try": MAX_TRIES}, *job_args, webhook_event_id=str(event.id)),
            timeout=timeout,
        )
    except TimeoutError:
        log.warning("webhook_inline_timeout", extra={"event_id": str(event.id)})
        return Receipt("accepted", event.id)
    except Exception:
        log.exception("webhook_inline_failed", extra={"event_id": str(event.id)})
        return Receipt("accepted", event.id)
    return Receipt("processed", event.id)
