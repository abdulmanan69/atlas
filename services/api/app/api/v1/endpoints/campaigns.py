"""Outbound calling campaigns: definition, preview, lifecycle, progress."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.v1.deps import DB, CampaignsManage, CampaignsRead
from app.models.campaigns import CampaignLeadStatus, CampaignStatus
from app.schemas.campaigns import (
    CampaignCreate,
    CampaignLeadOut,
    CampaignLeadPerson,
    CampaignOut,
    CampaignPreview,
    CampaignProgress,
    CampaignUpdate,
)
from app.schemas.common import ERROR_RESPONSES, ErrorResponse, Page
from app.services import campaigns as service

router = APIRouter(
    prefix="/campaigns",
    tags=["Campaigns"],
    responses={
        **ERROR_RESPONSES,
        409: {"model": ErrorResponse, "description": "Not allowed in the campaign's state"},
    },
)


@router.get("", response_model=Page[CampaignOut])
async def list_campaigns(
    ctx: CampaignsRead,
    db: DB,
    status_filter: Annotated[CampaignStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[CampaignOut]:
    items, total = await service.list_campaigns(
        db, ctx, status=status_filter, page=page, page_size=page_size
    )
    return Page(
        items=[CampaignOut.model_validate(c) for c in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=CampaignOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a campaign as a draft (dry run unless mode is live)",
)
async def create_campaign(data: CampaignCreate, ctx: CampaignsManage, db: DB) -> CampaignOut:
    return CampaignOut.model_validate(await service.create_campaign(db, ctx, data))


@router.get("/{campaign_id}", response_model=CampaignOut)
async def get_campaign(campaign_id: uuid.UUID, ctx: CampaignsRead, db: DB) -> CampaignOut:
    return CampaignOut.model_validate(await service.get_campaign(db, ctx, campaign_id))


@router.patch(
    "/{campaign_id}",
    response_model=CampaignOut,
    summary="Edit a campaign (agent, mode and audience are frozen once started)",
)
async def update_campaign(
    campaign_id: uuid.UUID, data: CampaignUpdate, ctx: CampaignsManage, db: DB
) -> CampaignOut:
    return CampaignOut.model_validate(await service.update_campaign(db, ctx, campaign_id, data))


@router.delete(
    "/{campaign_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a draft or finished campaign (call history is kept)",
)
async def delete_campaign(campaign_id: uuid.UUID, ctx: CampaignsManage, db: DB) -> None:
    await service.delete_campaign(db, ctx, campaign_id)


@router.post(
    "/{campaign_id}/preview",
    response_model=CampaignPreview,
    summary="Who would be called, who would be skipped and why; no side effects",
)
async def preview(campaign_id: uuid.UUID, ctx: CampaignsRead, db: DB) -> CampaignPreview:
    return await service.preview(db, ctx, campaign_id)


@router.post(
    "/{campaign_id}/start",
    response_model=CampaignOut,
    summary="Snapshot the audience and run (live mode needs campaigns:launch_live "
    "and ATLAS_LIVE_CALLS_ENABLED)",
)
async def start(campaign_id: uuid.UUID, ctx: CampaignsManage, db: DB) -> CampaignOut:
    return CampaignOut.model_validate(await service.start(db, ctx, campaign_id))


@router.post("/{campaign_id}/pause", response_model=CampaignOut)
async def pause(campaign_id: uuid.UUID, ctx: CampaignsManage, db: DB) -> CampaignOut:
    return CampaignOut.model_validate(await service.pause(db, ctx, campaign_id))


@router.post("/{campaign_id}/resume", response_model=CampaignOut)
async def resume(campaign_id: uuid.UUID, ctx: CampaignsManage, db: DB) -> CampaignOut:
    return CampaignOut.model_validate(await service.resume(db, ctx, campaign_id))


@router.post(
    "/{campaign_id}/cancel",
    response_model=CampaignOut,
    summary="Stop dispatching; calls already in progress finish and are recorded",
)
async def cancel(campaign_id: uuid.UUID, ctx: CampaignsManage, db: DB) -> CampaignOut:
    return CampaignOut.model_validate(await service.cancel(db, ctx, campaign_id))


@router.get("/{campaign_id}/progress", response_model=CampaignProgress)
async def progress(campaign_id: uuid.UUID, ctx: CampaignsRead, db: DB) -> CampaignProgress:
    return await service.progress(db, ctx, campaign_id)


@router.get(
    "/{campaign_id}/leads",
    response_model=Page[CampaignLeadOut],
    summary="The campaign's audience snapshot with per-lead status",
)
async def campaign_leads(
    campaign_id: uuid.UUID,
    ctx: CampaignsRead,
    db: DB,
    status_filter: Annotated[CampaignLeadStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[CampaignLeadOut]:
    rows, total = await service.list_campaign_leads(
        db, ctx, campaign_id, status=status_filter, page=page, page_size=page_size
    )
    return Page(
        items=[
            CampaignLeadOut(
                **{
                    k: getattr(item, k)
                    for k in (
                        "id",
                        "lead_id",
                        "status",
                        "attempts",
                        "next_attempt_at",
                        "last_call_id",
                        "last_outcome",
                        "skip_reason",
                        "updated_at",
                    )
                },
                lead=CampaignLeadPerson.model_validate(lead) if lead else None,
            )
            for item, lead in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
