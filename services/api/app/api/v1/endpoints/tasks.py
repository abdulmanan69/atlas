"""Follow-up tasks."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.v1.deps import DB, LeadsRead, TasksWrite
from app.models.crm import TaskStatus
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.crm import TaskCreate, TaskOut, TaskUpdate
from app.services import tasks as task_service

router = APIRouter(prefix="/tasks", tags=["Tasks"], responses=ERROR_RESPONSES)


@router.get("", response_model=Page[TaskOut])
async def list_tasks(
    ctx: LeadsRead,
    db: DB,
    status_: Annotated[TaskStatus | None, Query(alias="status")] = None,
    assignee_user_id: uuid.UUID | None = None,
    mine: bool = False,
    lead_id: uuid.UUID | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[TaskOut]:
    items, total = await task_service.list_tasks(
        db,
        ctx,
        status=status_,
        assignee_user_id=ctx.user_id if mine else assignee_user_id,
        lead_id=lead_id,
        page=page,
        page_size=page_size,
    )
    return Page(
        items=[TaskOut.model_validate(t) for t in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(data: TaskCreate, ctx: TasksWrite, db: DB) -> TaskOut:
    return TaskOut.model_validate(await task_service.create_task(db, ctx, data))


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(task_id: uuid.UUID, data: TaskUpdate, ctx: TasksWrite, db: DB) -> TaskOut:
    return TaskOut.model_validate(await task_service.update_task(db, ctx, task_id, data))


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(task_id: uuid.UUID, ctx: TasksWrite, db: DB) -> None:
    await task_service.delete_task(db, ctx, task_id)
