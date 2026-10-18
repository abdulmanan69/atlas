"""Leads, activity timeline, notes, bulk actions, CSV import/export."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, UploadFile, status
from fastapi.responses import StreamingResponse

from app.api.v1.deps import DB, LeadsDelete, LeadsExport, LeadsImport, LeadsRead, LeadsWrite
from app.core.exceptions import ValidationFailedError
from app.core.permissions import Permission
from app.models.crm import QualificationStatus
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.crm import (
    ActivityOut,
    BulkLeadRequest,
    BulkLeadResult,
    ImportReport,
    LeadCreate,
    LeadOut,
    LeadSort,
    LeadUpdate,
    NoteCreate,
)
from app.services import leads as lead_service

router = APIRouter(prefix="/leads", tags=["Leads"], responses=ERROR_RESPONSES)
MAX_IMPORT_BYTES = 5 * 1024 * 1024


def _filters(
    q: Annotated[
        str | None, Query(max_length=200, description="Name, email, company, phone")
    ] = None,
    stage_id: uuid.UUID | None = None,
    owner_user_id: uuid.UUID | None = None,
    unassigned: bool = False,
    tag: Annotated[str | None, Query(max_length=64)] = None,
    qualification_status: QualificationStatus | None = None,
    do_not_call: bool | None = None,
    source: Annotated[str | None, Query(max_length=80)] = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
) -> lead_service.LeadFilters:
    return lead_service.LeadFilters(
        q=q,
        stage_id=stage_id,
        owner_user_id=owner_user_id,
        unassigned=unassigned,
        tag=tag,
        qualification_status=qualification_status,
        do_not_call=do_not_call,
        source=source,
        created_from=created_from,
        created_to=created_to,
    )


Filters = Annotated[lead_service.LeadFilters, Depends(_filters)]


@router.get("", response_model=Page[LeadOut], summary="Search and filter leads")
async def list_leads(
    ctx: LeadsRead,
    db: DB,
    f: Filters,
    sort: LeadSort = LeadSort.CREATED_DESC,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> Page[LeadOut]:
    items, total = await lead_service.list_leads(db, ctx, f, sort, page, page_size)
    return Page(
        items=[LeadOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=LeadOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a lead (phone normalized to E.164, duplicates rejected)",
)
async def create_lead(data: LeadCreate, ctx: LeadsWrite, db: DB) -> LeadOut:
    return LeadOut.model_validate(await lead_service.create_lead(db, ctx, data))


@router.post("/bulk", response_model=BulkLeadResult, summary="Apply an action to many leads")
async def bulk(req: BulkLeadRequest, ctx: LeadsWrite, db: DB) -> BulkLeadResult:
    if req.action.value == "delete":
        ctx.require(Permission.LEADS_DELETE)
    return BulkLeadResult(affected=await lead_service.bulk_action(db, ctx, req))


@router.post(
    "/import",
    response_model=ImportReport,
    summary="Import leads from CSV (use dry_run=true to validate first)",
)
async def import_leads(
    ctx: LeadsImport,
    db: DB,
    file: Annotated[UploadFile, File(description="UTF-8 CSV with a header row")],
    dry_run: bool = True,
    source: Annotated[str | None, Query(max_length=80)] = "csv_import",
) -> ImportReport:
    content = await file.read(MAX_IMPORT_BYTES + 1)
    if len(content) > MAX_IMPORT_BYTES:
        raise ValidationFailedError("CSV file exceeds 5 MB")
    return await lead_service.import_csv(db, ctx, content, dry_run=dry_run, source=source)


@router.get(
    "/export",
    summary="Stream matching leads as CSV",
    response_class=StreamingResponse,
    responses={200: {"content": {"text/csv": {}}}},
)
async def export_leads(ctx: LeadsExport, db: DB, f: Filters) -> StreamingResponse:
    return StreamingResponse(
        lead_service.export_csv(db, ctx, f),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )


@router.get("/{lead_id}", response_model=LeadOut)
async def get_lead(lead_id: uuid.UUID, ctx: LeadsRead, db: DB) -> LeadOut:
    return LeadOut.model_validate(await lead_service.get_lead(db, ctx, lead_id))


@router.patch("/{lead_id}", response_model=LeadOut)
async def update_lead(lead_id: uuid.UUID, data: LeadUpdate, ctx: LeadsWrite, db: DB) -> LeadOut:
    return LeadOut.model_validate(await lead_service.update_lead(db, ctx, lead_id, data))


@router.delete(
    "/{lead_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Archive a lead (history retained)",
)
async def delete_lead(lead_id: uuid.UUID, ctx: LeadsDelete, db: DB) -> None:
    await lead_service.delete_lead(db, ctx, lead_id)


@router.get("/{lead_id}/activities", response_model=Page[ActivityOut], summary="Lead timeline")
async def timeline(
    lead_id: uuid.UUID,
    ctx: LeadsRead,
    db: DB,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> Page[ActivityOut]:
    items, total = await lead_service.timeline(db, ctx, lead_id, page, page_size)
    return Page(
        items=[ActivityOut.model_validate(i) for i in items],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post("/{lead_id}/notes", response_model=ActivityOut, status_code=status.HTTP_201_CREATED)
async def add_note(lead_id: uuid.UUID, data: NoteCreate, ctx: LeadsWrite, db: DB) -> ActivityOut:
    return ActivityOut.model_validate(await lead_service.add_note(db, ctx, lead_id, data.body))
