"""Audit log writer. Entries share the caller's transaction."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import api_key_id_ctx, redact, request_id_ctx
from app.models.audit import AuditLog


async def record(
    db: AsyncSession,
    *,
    action: str,
    organization_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
    resource_type: str | None = None,
    resource_id: uuid.UUID | str | None = None,
    changes: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    recorded = dict(changes or {})
    # Requests authenticated with a tenant API key are attributed to the key as well as
    # to the member who created it (`actor_user_id`).
    if api_key_id := api_key_id_ctx.get():
        recorded.setdefault("actor", {"type": "api_key", "id": api_key_id})
    db.add(
        AuditLog(
            organization_id=organization_id,
            actor_user_id=actor_user_id,
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id) if resource_id is not None else None,
            changes=redact(recorded),
            ip_address=ip,
            user_agent=user_agent,
            request_id=request_id_ctx.get(),
        )
    )
