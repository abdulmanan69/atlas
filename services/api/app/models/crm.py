"""CRM domain: pipeline stages, leads, activities, tasks."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from app.models.identity import str_enum


class StageCategory(StrEnum):
    OPEN = "open"
    WON = "won"
    LOST = "lost"


class QualificationStatus(StrEnum):
    UNKNOWN = "unknown"
    QUALIFIED = "qualified"
    UNQUALIFIED = "unqualified"


class ConsentStatus(StrEnum):
    UNKNOWN = "unknown"
    GRANTED = "granted"
    REVOKED = "revoked"


class ActivityType(StrEnum):
    NOTE = "note"
    CALL = "call"
    STAGE_CHANGE = "stage_change"
    TASK = "task"
    IMPORT = "import"
    SYSTEM = "system"
    UPDATE = "update"


class ActorType(StrEnum):
    USER = "user"
    AI_AGENT = "ai_agent"
    SYSTEM = "system"


class TaskStatus(StrEnum):
    OPEN = "open"
    DONE = "done"
    CANCELLED = "cancelled"


class PipelineStage(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "pipeline_stages"
    __table_args__ = (UniqueConstraint("organization_id", "key"),)

    key: Mapped[str] = mapped_column(String(48), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    category: Mapped[StageCategory] = mapped_column(
        str_enum(StageCategory, "stage_category"), nullable=False, default=StageCategory.OPEN
    )
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    color: Mapped[str | None] = mapped_column(String(16))


DEFAULT_STAGES: list[tuple[str, str, StageCategory]] = [
    ("new", "New", StageCategory.OPEN),
    ("contacted", "Contacted", StageCategory.OPEN),
    ("qualified", "Qualified", StageCategory.OPEN),
    ("converted", "Converted", StageCategory.WON),
    ("disqualified", "Disqualified", StageCategory.LOST),
    ("unreachable", "Unreachable", StageCategory.LOST),
    ("opted_out", "Opted out", StageCategory.LOST),
]


class Lead(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "leads"
    __table_args__ = (
        # Dedup key: one live lead per normalized phone number per organization.
        Index(
            "uq_leads_org_phone_live",
            "organization_id",
            "phone_e164",
            unique=True,
            postgresql_where=text("deleted_at IS NULL AND phone_e164 IS NOT NULL"),
        ),
        Index("ix_leads_org_stage", "organization_id", "stage_id"),
        Index("ix_leads_org_owner", "organization_id", "owner_user_id"),
        Index("ix_leads_org_created", "organization_id", "created_at"),
        Index("ix_leads_org_followup", "organization_id", "next_follow_up_at"),
        Index("ix_leads_org_updated", "organization_id", "updated_at"),
        Index("ix_leads_org_last_contacted", "organization_id", "last_contacted_at"),
        Index("ix_leads_tags", "tags", postgresql_using="gin"),
        Index(
            "ix_leads_search_trgm",
            "search_text",
            postgresql_using="gin",
            postgresql_ops={"search_text": "gin_trgm_ops"},
        ),
    )

    first_name: Mapped[str] = mapped_column(String(120), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(120))
    phone_e164: Mapped[str | None] = mapped_column(String(20))
    phone_raw: Mapped[str | None] = mapped_column(String(64))
    email: Mapped[str | None] = mapped_column(String(320))
    company: Mapped[str | None] = mapped_column(String(200))
    job_title: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str | None] = mapped_column(String(80))
    tags: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default="{}"
    )
    custom_fields: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    stage_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pipeline_stages.id", ondelete="RESTRICT"), nullable=False
    )
    qualification_status: Mapped[QualificationStatus] = mapped_column(
        str_enum(QualificationStatus, "qualification_status"),
        nullable=False,
        default=QualificationStatus.UNKNOWN,
    )
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    consent_status: Mapped[ConsentStatus] = mapped_column(
        str_enum(ConsentStatus, "consent_status"), nullable=False, default=ConsentStatus.UNKNOWN
    )
    do_not_call: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    timezone: Mapped[str | None] = mapped_column(String(64))
    next_follow_up_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_contacted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_text: Mapped[str] = mapped_column(
        Text,
        Computed(
            "lower(coalesce(first_name,'') || ' ' || coalesce(last_name,'') || ' ' || "
            "coalesce(email,'') || ' ' || coalesce(company,'') || ' ' || coalesce(phone_e164,''))",
            persisted=True,
        ),
    )


class Activity(UUIDPrimaryKey, TenantScoped, Base):
    """Append-only timeline entry for a lead."""

    __tablename__ = "activities"
    __table_args__ = (
        Index("ix_activities_lead_time", "organization_id", "lead_id", "occurred_at"),
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    type: Mapped[ActivityType] = mapped_column(
        str_enum(ActivityType, "activity_type"), nullable=False
    )
    actor_type: Mapped[ActorType] = mapped_column(str_enum(ActorType, "actor_type"), nullable=False)
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Task(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_org_assignee_status", "organization_id", "assignee_user_id", "status"),
        Index("ix_tasks_org_due", "organization_id", "due_at"),
        Index("ix_tasks_org_status_due", "organization_id", "status", "due_at"),
    )

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[TaskStatus] = mapped_column(
        str_enum(TaskStatus, "task_status"), nullable=False, default=TaskStatus.OPEN
    )
    assignee_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
