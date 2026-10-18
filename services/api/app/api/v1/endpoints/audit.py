"""Audit log (read-only)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.v1.deps import DB, AuditRead
from app.models.audit import AuditLog
from app.schemas.common import ERROR_RESPONSES, APIModel, Page

router = APIRouter(prefix="/audit-logs", tags=["Audit"], responses=ERROR_RESPONSES)


class AuditLogOut(APIModel):
    id: uuid.UUID
    actor_user_id: uuid.UUID | None
    action: str
    resource_type: str | None
    resource_id: str | None
    changes: dict[str, Any]
    ip_address: str | None
    request_id: str | None
    created_at: datetime


@router.get("", response_model=Page[AuditLogOut])
async def list_audit_logs(
    ctx: AuditRead,
    db: DB,
    action: Annotated[str | None, Query(max_length=80)] = None,
    actor_user_id: uuid.UUID | None = None,
    resource_type: Annotated[str | None, Query(max_length=48)] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[AuditLogOut]:
    stmt = select(AuditLog).where(AuditLog.organization_id == ctx.org_id)
    if action:
        stmt = stmt.where(AuditLog.action.startswith(action))
    if actor_user_id:
        stmt = stmt.where(AuditLog.actor_user_id == actor_user_id)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    total = int(await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = (
        (
            await db.execute(
                stmt.order_by(AuditLog.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        .scalars()
        .all()
    )
    items = [
        AuditLogOut(
            id=r.id,
            actor_user_id=r.actor_user_id,
            action=r.action,
            resource_type=r.resource_type,
            resource_id=r.resource_id,
            changes=r.changes,
            ip_address=str(r.ip_address) if r.ip_address else None,
            request_id=r.request_id,
            created_at=r.created_at,
        )
        for r in rows
    ]
    return Page(items=items, total=total, page=page, page_size=page_size)
