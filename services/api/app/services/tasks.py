"""Follow-up tasks."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.exceptions import NotFoundError, ValidationFailedError
from app.db.pagination import page_scalars
from app.models.crm import Activity, ActivityType, ActorType, Task, TaskStatus
from app.models.identity import Membership
from app.schemas.crm import TaskCreate, TaskUpdate
from app.services import leads as lead_service


async def _assert_member(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    found = await db.scalar(
        select(Membership.id).where(
            Membership.organization_id == org_id, Membership.user_id == user_id
        )
    )
    if not found:
        raise ValidationFailedError("Assignee is not a member of this organization")


async def list_tasks(
    db: AsyncSession,
    ctx: TenantContext,
    *,
    status: TaskStatus | None,
    assignee_user_id: uuid.UUID | None,
    lead_id: uuid.UUID | None,
    page: int,
    page_size: int,
) -> tuple[list[Task], int]:
    stmt = select(Task).where(Task.organization_id == ctx.org_id)
    if status:
        stmt = stmt.where(Task.status == status)
    if assignee_user_id:
        stmt = stmt.where(Task.assignee_user_id == assignee_user_id)
    if lead_id:
        stmt = stmt.where(Task.lead_id == lead_id)
    # Task.id breaks ties so offset pages never repeat or skip a row.
    ordered = stmt.order_by(Task.due_at.asc().nulls_last(), Task.created_at.desc(), Task.id)
    return await page_scalars(db, ordered, page, page_size)


async def _get(db: AsyncSession, ctx: TenantContext, task_id: uuid.UUID) -> Task:
    task = (
        await db.execute(select(Task).where(Task.id == task_id, Task.organization_id == ctx.org_id))
    ).scalar_one_or_none()
    if task is None:
        raise NotFoundError("Task not found")
    return task


async def create_task(db: AsyncSession, ctx: TenantContext, data: TaskCreate) -> Task:
    if data.lead_id:
        await lead_service.get_lead(db, ctx, data.lead_id)
    if data.assignee_user_id:
        await _assert_member(db, ctx.org_id, data.assignee_user_id)
    fields = data.model_dump()
    # Unassigned tasks default to their creator so nothing is orphaned.
    fields["assignee_user_id"] = data.assignee_user_id or ctx.user_id
    task = Task(organization_id=ctx.org_id, created_by_user_id=ctx.user_id, **fields)
    db.add(task)
    await db.flush()
    if task.lead_id:
        db.add(
            Activity(
                organization_id=ctx.org_id,
                lead_id=task.lead_id,
                type=ActivityType.TASK,
                actor_type=ActorType.USER,
                actor_user_id=ctx.user_id,
                summary=f"Task created: {task.title}"[:500],
                data={"task_id": str(task.id)},
            )
        )
    await db.flush()
    await db.refresh(task)
    return task


async def update_task(
    db: AsyncSession, ctx: TenantContext, task_id: uuid.UUID, data: TaskUpdate
) -> Task:
    task = await _get(db, ctx, task_id)
    changes = data.model_dump(exclude_unset=True)
    if changes.get("assignee_user_id"):
        await _assert_member(db, ctx.org_id, changes["assignee_user_id"])
    completing = changes.get("status") == TaskStatus.DONE and task.status != TaskStatus.DONE
    for field, value in changes.items():
        setattr(task, field, value)
    if completing:
        task.completed_at = datetime.now(UTC)
        if task.lead_id:
            db.add(
                Activity(
                    organization_id=ctx.org_id,
                    lead_id=task.lead_id,
                    type=ActivityType.TASK,
                    actor_type=ActorType.USER,
                    actor_user_id=ctx.user_id,
                    summary=f"Task completed: {task.title}"[:500],
                    data={"task_id": str(task.id)},
                )
            )
    elif changes.get("status") == TaskStatus.OPEN:
        task.completed_at = None
    await db.flush()
    await db.refresh(task)
    return task


async def delete_task(db: AsyncSession, ctx: TenantContext, task_id: uuid.UUID) -> None:
    task = await _get(db, ctx, task_id)
    await db.delete(task)
