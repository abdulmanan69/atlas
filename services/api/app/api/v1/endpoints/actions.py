"""Actions: HTTP calls an agent can make during a conversation."""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.v1.deps import DB, AgentsManage, AgentsRead
from app.schemas.actions import (
    ActionCreate,
    ActionOut,
    ActionTestRequest,
    ActionTestResult,
    ActionUpdate,
)
from app.schemas.common import ERROR_RESPONSES
from app.services import actions as service

router = APIRouter(prefix="/actions", tags=["Actions"], responses=ERROR_RESPONSES)


@router.get("", response_model=list[ActionOut], summary="Actions available to this organization")
async def list_actions(ctx: AgentsRead, db: DB) -> list[ActionOut]:
    return await service.list_actions(db, ctx)


@router.post(
    "",
    response_model=ActionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an action (the URL must pass the outbound destination policy)",
)
async def create_action(data: ActionCreate, ctx: AgentsManage, db: DB) -> ActionOut:
    return await service.create_action(db, ctx, data)


@router.get("/{action_id}", response_model=ActionOut)
async def get_action(action_id: str, ctx: AgentsRead, db: DB) -> ActionOut:
    return await service.get_action(db, ctx, action_id)


@router.patch(
    "/{action_id}",
    response_model=ActionOut,
    summary="Change an action; send auth: null to remove its credential",
)
async def update_action(action_id: str, data: ActionUpdate, ctx: AgentsManage, db: DB) -> ActionOut:
    return await service.update_action(db, ctx, action_id, data)


@router.delete("/{action_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_action(action_id: str, ctx: AgentsManage, db: DB) -> None:
    await service.delete_action(db, ctx, action_id)


@router.post(
    "/{action_id}/test",
    response_model=ActionTestResult,
    summary="Run the action now with sample values and see the response",
)
async def test_action(
    action_id: str, data: ActionTestRequest, ctx: AgentsManage, db: DB
) -> ActionTestResult:
    return await service.test_action(db, ctx, action_id, data)
