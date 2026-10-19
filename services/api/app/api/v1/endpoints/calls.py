"""Calls: start one, browse history, inspect results, transcripts and recordings."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import DB, CallsRead, CampaignsManage, RecordingsRead
from app.models.calls import Call, CallDirection, CallMode, CallState
from app.models.crm import Lead
from app.schemas.calls import CallDetail, CallEventOut, CallOut, CallStart, CallTranscript
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.latency import CallLatencyDetail
from app.services import calls as service

router = APIRouter(tags=["Calls"], responses=ERROR_RESPONSES)

_EXTENSIONS = {"audio/wav": "wav", "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/mp4": "m4a"}


def _filters(
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    agent_id: uuid.UUID | None = None,
    campaign_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    state: CallState | None = None,
    outcome: Annotated[str | None, Query(max_length=64)] = None,
    mode: CallMode | None = None,
    direction: CallDirection | None = None,
) -> service.CallFilters:
    return service.CallFilters(
        created_from=created_from,
        created_to=created_to,
        agent_id=agent_id,
        campaign_id=campaign_id,
        lead_id=lead_id,
        state=state,
        outcome=outcome,
        mode=mode,
        direction=direction,
    )


Filters = Annotated[service.CallFilters, Depends(_filters)]


async def _with_lead_names[OutT: CallOut](
    db: AsyncSession, org_id: uuid.UUID, calls: list[Call], model: type[OutT]
) -> list[OutT]:
    """Serialize calls and attach each lead's current display name in one extra query."""
    ids = {c.lead_id for c in calls if c.lead_id is not None}
    names: dict[uuid.UUID, str] = {}
    if ids:
        rows = await db.execute(
            select(Lead.id, Lead.first_name, Lead.last_name).where(
                Lead.organization_id == org_id, Lead.id.in_(ids)
            )
        )
        names = {lead_id: f"{first} {last}" if last else first for lead_id, first, last in rows}
    out: list[OutT] = []
    for call in calls:
        item = model.model_validate(call)
        item.lead_name = names.get(call.lead_id) if call.lead_id else None
        out.append(item)
    return out


@router.post(
    "/calls",
    response_model=CallOut,
    status_code=status.HTTP_201_CREATED,
    summary="Start a call to a lead with an agent (simulated unless live=true)",
)
async def start_call(data: CallStart, ctx: CampaignsManage, db: DB) -> CallOut:
    call = await service.start_call(
        db, ctx, lead_id=data.lead_id, agent_id=data.agent_id, live=data.live
    )
    return CallOut.model_validate(call)


@router.get("/calls", response_model=Page[CallOut], summary="Call history, newest first")
async def list_calls(
    ctx: CallsRead,
    db: DB,
    f: Filters,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> Page[CallOut]:
    items, total = await service.list_calls(db, ctx, f, page, page_size)
    return Page(
        items=await _with_lead_names(db, ctx.org_id, items, CallOut),
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/calls/{call_id}", response_model=CallDetail)
async def get_call(call_id: uuid.UUID, ctx: CallsRead, db: DB) -> CallDetail:
    call = await service.get_call(db, ctx, call_id)
    return (await _with_lead_names(db, ctx.org_id, [call], CallDetail))[0]


@router.get(
    "/calls/{call_id}/events",
    response_model=list[CallEventOut],
    summary="What happened to this call, in order",
)
async def call_events(call_id: uuid.UUID, ctx: CallsRead, db: DB) -> list[CallEventOut]:
    return [CallEventOut.model_validate(e) for e in await service.list_events(db, ctx, call_id)]


@router.get(
    "/calls/{call_id}/latency",
    response_model=CallLatencyDetail,
    summary="Response times per turn (404 when the call was not measured)",
)
async def call_latency(call_id: uuid.UUID, ctx: CallsRead, db: DB) -> CallLatencyDetail:
    stored = await service.get_latency(db, ctx, call_id)
    return CallLatencyDetail.model_validate(
        {"summary": stored["summary"], "turns": stored.get("turns") or []}
    )


@router.get(
    "/calls/{call_id}/transcript",
    response_model=CallTranscript,
    summary="Conversation transcript (fetched once, then stored with the call)",
)
async def transcript(call_id: uuid.UUID, ctx: CallsRead, db: DB) -> CallTranscript:
    call, text = await service.get_transcript(db, ctx, call_id)
    return CallTranscript(
        call_id=call.id, mode=call.mode, simulated=call.mode == CallMode.SIMULATED, text=text
    )


@router.get(
    "/calls/{call_id}/recording",
    summary="Stream the call recording (access is audited)",
    response_class=StreamingResponse,
    responses={200: {"content": {"audio/wav": {}, "audio/mpeg": {}}}},
)
async def recording(call_id: uuid.UUID, ctx: RecordingsRead, db: DB) -> StreamingResponse:
    call, stream = await service.open_recording(db, ctx, call_id)
    extension = _EXTENSIONS.get(stream.content_type.split(";")[0].strip(), "bin")
    headers = {
        "Content-Disposition": f'inline; filename="call-{call.id}.{extension}"',
        "Cache-Control": "private, no-store",
    }
    if stream.content_length is not None:
        headers["Content-Length"] = str(stream.content_length)
    return StreamingResponse(stream.chunks, media_type=stream.content_type, headers=headers)


@router.get(
    "/leads/{lead_id}/calls",
    response_model=Page[CallOut],
    summary="Calls with one lead, newest first",
)
async def lead_calls(
    lead_id: uuid.UUID,
    ctx: CallsRead,
    db: DB,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 25,
) -> Page[CallOut]:
    items, total = await service.list_lead_calls(db, ctx, lead_id, page, page_size)
    return Page(
        items=await _with_lead_names(db, ctx.org_id, items, CallOut),
        total=total,
        page=page,
        page_size=page_size,
    )
