"""Domain events for outbound integrations (transactional outbox).

`emit` is called by the CRM, call and campaign services when something a customer
system may want to know about happens (lead.created, call.completed, ...). The event
is written to `outbox_events` inside the caller's transaction, so it exists exactly
when the business change was committed. The worker in
`app/workers/outbound_webhooks.py` fans events out to subscribed webhook endpoints
and delivers them with retries. The `emit` signature is the contract.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import redact
from app.models.developer import OutboxEvent

log = logging.getLogger(__name__)

EVENT_TYPES: tuple[str, ...] = (
    "lead.created",
    "lead.updated",
    "lead.stage_changed",
    "call.started",
    "call.completed",
    "campaign.started",
    "campaign.completed",
)


def _json_default(value: object) -> object:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Enum):
        return value.value
    return str(value)


def jsonable(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce ids, timestamps and enums so the payload round-trips through JSONB."""
    result: dict[str, Any] = json.loads(json.dumps(data, default=_json_default))
    return result


async def emit(
    db: AsyncSession, organization_id: uuid.UUID, event_type: str, data: dict[str, Any]
) -> None:
    """Record an event for delivery. Shares the caller's transaction.

    `data` must not contain secrets; keys that look like credentials are masked anyway.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unknown event type: {event_type}")
    db.add(
        OutboxEvent(
            organization_id=organization_id,
            event_type=event_type,
            data=redact(jsonable(data)),
        )
    )
    log.debug(
        "event_emitted",
        extra={"event_type": event_type, "organization_id": str(organization_id)},
    )
