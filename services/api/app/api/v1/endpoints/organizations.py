"""Organization settings, members, invitations, pipeline stages."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status
from sqlalchemy import func, select

from app.api.v1.deps import DB, AuthUser, LeadsRead, MembersManage, MembersRead, OrgManage, OrgRead
from app.core.exceptions import ConflictError
from app.models.crm import PipelineStage
from app.schemas.common import ERROR_RESPONSES
from app.schemas.crm import StageCreate, StageOut
from app.schemas.identity import (
    InvitationCreate,
    InvitationCreated,
    InvitationOut,
    MemberOut,
    MembershipOut,
    MemberUpdate,
    OrganizationCreate,
    OrganizationOut,
    OrganizationUpdate,
)
from app.services import leads as lead_service
from app.services import organizations as org_service

router = APIRouter(tags=["Organizations"], responses=ERROR_RESPONSES)


@router.post(
    "/organizations",
    response_model=MembershipOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create another organization you will own",
)
async def create_organization(data: OrganizationCreate, current: AuthUser, db: DB) -> MembershipOut:
    org = await org_service.create_organization(db, current.user, data)
    memberships = await org_service.list_memberships(db, current.user.id)
    return MembershipOut.model_validate(next(m for m in memberships if m.organization_id == org.id))


@router.get(
    "/organization",
    response_model=OrganizationOut,
    summary="Current organization (from X-Organization-Id)",
)
async def get_organization(ctx: OrgRead) -> OrganizationOut:
    return OrganizationOut.model_validate(ctx.organization)


@router.patch("/organization", response_model=OrganizationOut)
async def update_organization(data: OrganizationUpdate, ctx: OrgManage, db: DB) -> OrganizationOut:
    return OrganizationOut.model_validate(await org_service.update_organization(db, ctx, data))


@router.get("/members", response_model=list[MemberOut], tags=["Members"])
async def list_members(ctx: MembersRead, db: DB) -> list[MemberOut]:
    return [MemberOut.model_validate(m) for m in await org_service.list_members(db, ctx)]


@router.patch("/members/{member_id}", response_model=MemberOut, tags=["Members"])
async def update_member(
    member_id: uuid.UUID, data: MemberUpdate, ctx: MembersManage, db: DB
) -> MemberOut:
    return MemberOut.model_validate(await org_service.update_member(db, ctx, member_id, data))


@router.delete("/members/{member_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Members"])
async def remove_member(member_id: uuid.UUID, ctx: MembersManage, db: DB) -> None:
    await org_service.remove_member(db, ctx, member_id)


@router.get("/invitations", response_model=list[InvitationOut], tags=["Members"])
async def list_invitations(ctx: MembersManage, db: DB) -> list[InvitationOut]:
    return [InvitationOut.model_validate(i) for i in await org_service.list_invitations(db, ctx)]


@router.post(
    "/invitations",
    response_model=InvitationCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["Members"],
)
async def create_invitation(
    data: InvitationCreate, ctx: MembersManage, db: DB
) -> InvitationCreated:
    inv, token = await org_service.create_invitation(db, ctx, data)
    return InvitationCreated(**InvitationOut.model_validate(inv).model_dump(), token=token)


@router.delete(
    "/invitations/{invitation_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["Members"]
)
async def revoke_invitation(invitation_id: uuid.UUID, ctx: MembersManage, db: DB) -> None:
    await org_service.revoke_invitation(db, ctx, invitation_id)


@router.get("/pipeline-stages", response_model=list[StageOut], tags=["Leads"])
async def list_stages(ctx: LeadsRead, db: DB) -> list[StageOut]:
    return [StageOut.model_validate(s) for s in await lead_service.list_stages(db, ctx.org_id)]


@router.post(
    "/pipeline-stages", response_model=StageOut, status_code=status.HTTP_201_CREATED, tags=["Leads"]
)
async def create_stage(data: StageCreate, ctx: OrgManage, db: DB) -> StageOut:
    exists = await db.scalar(
        select(PipelineStage.id).where(
            PipelineStage.organization_id == ctx.org_id, PipelineStage.key == data.key
        )
    )
    if exists:
        raise ConflictError("A stage with that key already exists")
    position = await db.scalar(
        select(func.coalesce(func.max(PipelineStage.position), -1) + 1).where(
            PipelineStage.organization_id == ctx.org_id
        )
    )
    stage = PipelineStage(
        organization_id=ctx.org_id, position=int(position or 0), **data.model_dump()
    )
    db.add(stage)
    await db.flush()
    return StageOut.model_validate(stage)
