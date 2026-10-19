"""Lead management: CRUD, search, stage transitions, timeline, CSV import/export."""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import ColumnElement, Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.exceptions import ConflictError, NotFoundError, ValidationFailedError
from app.core.phone import InvalidPhoneError, normalize_phone
from app.db.pagination import page_scalars
from app.models.crm import (
    Activity,
    ActivityType,
    ActorType,
    ConsentStatus,
    Lead,
    PipelineStage,
    QualificationStatus,
    StageCategory,
)
from app.models.identity import Membership
from app.schemas.crm import (
    BulkLeadAction,
    BulkLeadRequest,
    ImportReport,
    ImportRowError,
    LeadCreate,
    LeadSort,
    LeadUpdate,
)
from app.services import audit, events

MAX_IMPORT_ROWS = 10_000
CSV_COLUMNS = [
    "first_name",
    "last_name",
    "phone",
    "email",
    "company",
    "job_title",
    "source",
    "tags",
]
_SORTS: dict[LeadSort, ColumnElement[Any]] = {
    LeadSort.CREATED_DESC: Lead.created_at.desc(),
    LeadSort.CREATED_ASC: Lead.created_at.asc(),
    LeadSort.UPDATED_DESC: Lead.updated_at.desc(),
    LeadSort.NAME_ASC: Lead.first_name.asc(),
    LeadSort.FOLLOW_UP_ASC: Lead.next_follow_up_at.asc().nulls_last(),
    LeadSort.LAST_CONTACTED_DESC: Lead.last_contacted_at.desc().nulls_last(),
}


@dataclass(slots=True)
class LeadFilters:
    q: str | None = None
    stage_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    unassigned: bool = False
    tag: str | None = None
    qualification_status: QualificationStatus | None = None
    do_not_call: bool | None = None
    source: str | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None


def _live(org_id: uuid.UUID) -> Select[tuple[Lead]]:
    return select(Lead).where(Lead.organization_id == org_id, Lead.deleted_at.is_(None))


def _apply_filters(stmt: Select[Any], f: LeadFilters) -> Select[Any]:
    if f.q:
        term = f"%{f.q.strip().lower()}%"
        stmt = stmt.where(Lead.search_text.like(term))
    if f.stage_id:
        stmt = stmt.where(Lead.stage_id == f.stage_id)
    if f.unassigned:
        stmt = stmt.where(Lead.owner_user_id.is_(None))
    elif f.owner_user_id:
        stmt = stmt.where(Lead.owner_user_id == f.owner_user_id)
    if f.tag:
        stmt = stmt.where(Lead.tags.contains([f.tag.lower()]))
    if f.qualification_status:
        stmt = stmt.where(Lead.qualification_status == f.qualification_status)
    if f.do_not_call is not None:
        stmt = stmt.where(Lead.do_not_call.is_(f.do_not_call))
    if f.source:
        stmt = stmt.where(Lead.source == f.source)
    if f.created_from:
        stmt = stmt.where(Lead.created_at >= f.created_from)
    if f.created_to:
        stmt = stmt.where(Lead.created_at < f.created_to)
    return stmt


async def list_leads(
    db: AsyncSession, ctx: TenantContext, f: LeadFilters, sort: LeadSort, page: int, page_size: int
) -> tuple[list[Lead], int]:
    base = _apply_filters(_live(ctx.org_id), f)
    return await page_scalars(db, base.order_by(_SORTS[sort], Lead.id), page, page_size)


async def get_lead(db: AsyncSession, ctx: TenantContext, lead_id: uuid.UUID) -> Lead:
    lead = (await db.execute(_live(ctx.org_id).where(Lead.id == lead_id))).scalar_one_or_none()
    if lead is None:
        raise NotFoundError("Lead not found")
    return lead


async def list_stages(db: AsyncSession, org_id: uuid.UUID) -> list[PipelineStage]:
    rows = await db.execute(
        select(PipelineStage)
        .where(PipelineStage.organization_id == org_id)
        .order_by(PipelineStage.position)
    )
    return list(rows.scalars().all())


async def _stage(db: AsyncSession, org_id: uuid.UUID, stage_id: uuid.UUID | None) -> PipelineStage:
    stmt = select(PipelineStage).where(PipelineStage.organization_id == org_id)
    stmt = (
        stmt.where(PipelineStage.id == stage_id)
        if stage_id
        else stmt.where(PipelineStage.is_default.is_(True))
    )
    stage = (await db.execute(stmt)).scalars().first()
    if stage is None:
        raise ValidationFailedError("Unknown pipeline stage")
    return stage


async def _assert_member(db: AsyncSession, org_id: uuid.UUID, user_id: uuid.UUID) -> None:
    found = await db.scalar(
        select(Membership.id).where(
            Membership.organization_id == org_id, Membership.user_id == user_id
        )
    )
    if not found:
        raise ValidationFailedError("Assignee is not a member of this organization")


def _normalize(ctx: TenantContext, raw: str | None) -> str | None:
    if raw is None or not raw.strip():
        return None
    try:
        return normalize_phone(raw, ctx.organization.default_country)
    except InvalidPhoneError as exc:
        raise ValidationFailedError(str(exc), details={"field": "phone"}) from exc


async def _assert_phone_free(
    db: AsyncSession, org_id: uuid.UUID, phone: str | None, exclude: uuid.UUID | None = None
) -> None:
    if phone is None:
        return
    stmt = select(Lead.id).where(
        Lead.organization_id == org_id, Lead.phone_e164 == phone, Lead.deleted_at.is_(None)
    )
    if exclude:
        stmt = stmt.where(Lead.id != exclude)
    existing = await db.scalar(stmt)
    if existing:
        raise ConflictError(
            "A lead with this phone number already exists",
            details={"existing_lead_id": str(existing)},
        )


def _activity(
    ctx: TenantContext,
    lead_id: uuid.UUID,
    type_: ActivityType,
    summary: str,
    data: dict[str, Any] | None = None,
    body: str | None = None,
) -> Activity:
    return Activity(
        organization_id=ctx.org_id,
        lead_id=lead_id,
        type=type_,
        actor_type=ActorType.USER,
        actor_user_id=ctx.user_id,
        summary=summary[:500],
        body=body,
        data=data or {},
    )


def _lead_event(lead: Lead, stage_key: str) -> dict[str, Any]:
    """Webhook payload for a lead: identifiers, pipeline position and name/company only.

    Phone, email, notes and custom fields are deliberately excluded; integrations fetch
    them through the API with their own credentials if they need them.
    """
    return {
        "lead_id": lead.id,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company": lead.company,
        "stage": stage_key,
        "stage_id": lead.stage_id,
        "owner_user_id": lead.owner_user_id,
        "source": lead.source,
        "tags": list(lead.tags),
        "qualification_status": lead.qualification_status,
        "do_not_call": lead.do_not_call,
    }


async def create_lead(db: AsyncSession, ctx: TenantContext, data: LeadCreate) -> Lead:
    phone = _normalize(ctx, data.phone)
    await _assert_phone_free(db, ctx.org_id, phone)
    stage = await _stage(db, ctx.org_id, data.stage_id)
    if data.owner_user_id:
        await _assert_member(db, ctx.org_id, data.owner_user_id)
    lead = Lead(
        organization_id=ctx.org_id,
        **data.model_dump(exclude={"phone", "stage_id", "email"}),
        email=str(data.email).lower() if data.email else None,
        phone_e164=phone,
        phone_raw=data.phone,
        stage_id=stage.id,
        created_by_user_id=ctx.user_id,
    )
    db.add(lead)
    await db.flush()
    db.add(_activity(ctx, lead.id, ActivityType.SYSTEM, "Lead created"))
    await audit.record(
        db,
        action="lead.created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="lead",
        resource_id=lead.id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(lead)
    await events.emit(db, ctx.org_id, "lead.created", _lead_event(lead, stage.key))
    return lead


async def update_lead(
    db: AsyncSession, ctx: TenantContext, lead_id: uuid.UUID, data: LeadUpdate
) -> Lead:
    lead = await get_lead(db, ctx, lead_id)
    changes = data.model_dump(exclude_unset=True)
    tracked: dict[str, Any] = {}

    if "phone" in changes:
        raw = changes.pop("phone")
        phone = _normalize(ctx, raw)
        await _assert_phone_free(db, ctx.org_id, phone, exclude=lead.id)
        lead.phone_e164, lead.phone_raw = phone, raw
        tracked["phone"] = "changed"
    if "email" in changes:
        email = changes.pop("email")
        lead.email = str(email).lower() if email else None
        tracked["email"] = "changed"
    if "stage_id" in changes:
        new_stage_id = changes.pop("stage_id")
        if new_stage_id and new_stage_id != lead.stage_id:
            await _move_stage(db, ctx, lead, new_stage_id)
    if changes.get("owner_user_id"):
        await _assert_member(db, ctx.org_id, changes["owner_user_id"])
    if changes.get("consent_status") == ConsentStatus.REVOKED:
        # Revoked consent always implies do-not-call.
        changes["do_not_call"] = True

    for field, value in changes.items():
        if getattr(lead, field) != value:
            tracked[field] = value
            setattr(lead, field, value)

    if tracked:
        db.add(
            _activity(
                ctx, lead.id, ActivityType.UPDATE, "Lead updated", data={"fields": sorted(tracked)}
            )
        )
        await audit.record(
            db,
            action="lead.updated",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="lead",
            resource_id=lead.id,
            changes=_jsonable(tracked),
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )
    await db.flush()
    await db.refresh(lead)
    if tracked:
        stage = await _stage(db, ctx.org_id, lead.stage_id)
        await events.emit(
            db,
            ctx.org_id,
            "lead.updated",
            {**_lead_event(lead, stage.key), "changed_fields": sorted(tracked)},
        )
    return lead


def _jsonable(d: dict[str, Any]) -> dict[str, Any]:
    return {k: (str(v) if isinstance(v, (uuid.UUID, datetime)) else v) for k, v in d.items()}


async def _move_stage(
    db: AsyncSession, ctx: TenantContext, lead: Lead, stage_id: uuid.UUID
) -> None:
    old = await _stage(db, ctx.org_id, lead.stage_id)
    new = await _stage(db, ctx.org_id, stage_id)
    lead.stage_id = new.id
    if new.category == StageCategory.WON:
        lead.qualification_status = QualificationStatus.QUALIFIED
    db.add(
        _activity(
            ctx,
            lead.id,
            ActivityType.STAGE_CHANGE,
            f"Stage changed: {old.name} → {new.name}",
            data={
                "from_stage_id": str(old.id),
                "to_stage_id": str(new.id),
                "from": old.key,
                "to": new.key,
            },
        )
    )
    await events.emit(
        db,
        ctx.org_id,
        "lead.stage_changed",
        {
            "lead_id": lead.id,
            "from_stage": old.key,
            "to_stage": new.key,
            "from_stage_id": old.id,
            "to_stage_id": new.id,
        },
    )


async def delete_lead(db: AsyncSession, ctx: TenantContext, lead_id: uuid.UUID) -> None:
    """Soft delete: call and audit history referencing the lead is retained."""
    lead = await get_lead(db, ctx, lead_id)
    lead.deleted_at = datetime.now(UTC)
    await audit.record(
        db,
        action="lead.deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="lead",
        resource_id=lead.id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


async def bulk_action(db: AsyncSession, ctx: TenantContext, req: BulkLeadRequest) -> int:
    ids_stmt = _live(ctx.org_id).where(Lead.id.in_(req.lead_ids))
    leads = list((await db.execute(ids_stmt)).scalars().all())
    if not leads:
        return 0
    if req.action == BulkLeadAction.ASSIGN:
        if req.owner_user_id:
            await _assert_member(db, ctx.org_id, req.owner_user_id)
        for lead in leads:
            lead.owner_user_id = req.owner_user_id
    elif req.action == BulkLeadAction.MOVE_STAGE:
        if not req.stage_id:
            raise ValidationFailedError("stage_id is required")
        for lead in leads:
            if lead.stage_id != req.stage_id:
                await _move_stage(db, ctx, lead, req.stage_id)
    elif req.action == BulkLeadAction.ADD_TAGS:
        tags = sorted({t.strip().lower() for t in (req.tags or []) if t.strip()})
        if not tags:
            raise ValidationFailedError("tags are required")
        for lead in leads:
            lead.tags = sorted(set(lead.tags) | set(tags))
    elif req.action == BulkLeadAction.DELETE:
        await db.execute(
            update(Lead)
            .where(Lead.id.in_([lead.id for lead in leads]))
            .values(deleted_at=datetime.now(UTC))
        )
    await audit.record(
        db,
        action=f"lead.bulk_{req.action.value}",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="lead",
        changes={"count": len(leads), "lead_ids": [str(lead.id) for lead in leads][:50]},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    return len(leads)


async def add_note(db: AsyncSession, ctx: TenantContext, lead_id: uuid.UUID, body: str) -> Activity:
    lead = await get_lead(db, ctx, lead_id)
    first_line = body.strip().splitlines()[0] if body.strip() else "Note"
    activity = _activity(ctx, lead.id, ActivityType.NOTE, first_line[:200], body=body)
    db.add(activity)
    await db.flush()
    return activity


async def timeline(
    db: AsyncSession, ctx: TenantContext, lead_id: uuid.UUID, page: int, page_size: int
) -> tuple[list[Activity], int]:
    await get_lead(db, ctx, lead_id)
    base = select(Activity).where(
        Activity.organization_id == ctx.org_id, Activity.lead_id == lead_id
    )
    return await page_scalars(
        db, base.order_by(Activity.occurred_at.desc(), Activity.id), page, page_size
    )


# ---------------------------------------------------------------------------- CSV


async def import_csv(
    db: AsyncSession, ctx: TenantContext, content: bytes, *, dry_run: bool, source: str | None
) -> ImportReport:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValidationFailedError("CSV must be UTF-8 encoded") from exc
    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip().lower() for h in (reader.fieldnames or [])}
    if "first_name" not in headers or not ({"phone", "email"} & headers):
        raise ValidationFailedError(
            "CSV needs a first_name column and at least one of phone or email",
            details={"expected_columns": CSV_COLUMNS},
        )

    default_stage = await _stage(db, ctx.org_id, None)
    existing_phones = set(
        (
            await db.execute(
                select(Lead.phone_e164).where(
                    Lead.organization_id == ctx.org_id,
                    Lead.deleted_at.is_(None),
                    Lead.phone_e164.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    seen: set[str] = set()
    errors: list[ImportRowError] = []
    to_create: list[Lead] = []
    total = duplicates = 0

    for index, raw_row in enumerate(reader, start=1):
        total += 1
        if total > MAX_IMPORT_ROWS:
            raise ValidationFailedError(f"Import is limited to {MAX_IMPORT_ROWS} rows per file")
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw_row.items()}
        tags = [t for t in row.get("tags", "").replace(",", ";").split(";") if t.strip()]
        try:
            parsed = LeadCreate(
                first_name=row.get("first_name", ""),
                last_name=row.get("last_name") or None,
                phone=row.get("phone") or None,
                email=row.get("email") or None,
                company=row.get("company") or None,
                job_title=row.get("job_title") or None,
                source=row.get("source") or source,
                tags=tags,
            )
            phone = _normalize(ctx, parsed.phone)
        except ValidationError as exc:
            errors.append(
                ImportRowError(
                    row=index,
                    errors=[f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()],
                )
            )
            continue
        except ValidationFailedError as exc:
            errors.append(ImportRowError(row=index, errors=[f"phone: {exc.message}"]))
            continue
        if phone and (phone in existing_phones or phone in seen):
            duplicates += 1
            continue
        if phone:
            seen.add(phone)
        to_create.append(
            Lead(
                organization_id=ctx.org_id,
                first_name=parsed.first_name,
                last_name=parsed.last_name,
                phone_e164=phone,
                phone_raw=parsed.phone,
                email=str(parsed.email).lower() if parsed.email else None,
                company=parsed.company,
                job_title=parsed.job_title,
                source=parsed.source,
                tags=parsed.tags,
                custom_fields={},
                stage_id=default_stage.id,
                created_by_user_id=ctx.user_id,
            )
        )

    if not dry_run and to_create:
        db.add_all(to_create)
        await db.flush()
        db.add_all(
            Activity(
                organization_id=ctx.org_id,
                lead_id=lead.id,
                type=ActivityType.IMPORT,
                actor_type=ActorType.USER,
                actor_user_id=ctx.user_id,
                summary="Imported from CSV",
                data={"source": lead.source},
            )
            for lead in to_create
        )
        await audit.record(
            db,
            action="lead.imported",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="lead",
            changes={"created": len(to_create), "rows": total},
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )
    return ImportReport(
        total_rows=total,
        created=0 if dry_run else len(to_create),
        skipped_duplicates=duplicates,
        failed=len(errors),
        dry_run=dry_run,
        errors=errors[:500],
    )


def _csv_safe(value: object) -> str:
    """Neutralize spreadsheet formula injection in exported cells."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


async def export_csv(db: AsyncSession, ctx: TenantContext, f: LeadFilters) -> AsyncIterator[str]:
    stages = {s.id: s.key for s in await list_stages(db, ctx.org_id)}
    columns = [*CSV_COLUMNS, "stage", "qualification_status", "do_not_call", "created_at"]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    yield buf.getvalue()
    stmt = _apply_filters(_live(ctx.org_id), f).order_by(Lead.created_at, Lead.id)
    result = await db.stream(stmt.execution_options(yield_per=500))
    async for lead in result.scalars():
        buf.seek(0)
        buf.truncate()
        writer.writerow(
            _csv_safe(v)
            for v in (
                lead.first_name,
                lead.last_name,
                lead.phone_e164,
                lead.email,
                lead.company,
                lead.job_title,
                lead.source,
                ";".join(lead.tags),
                stages.get(lead.stage_id),
                lead.qualification_status.value,
                lead.do_not_call,
                lead.created_at.isoformat(),
            )
        )
        yield buf.getvalue()
