"""CRM contracts: stages, leads, activities, tasks, imports."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import EmailStr, Field, computed_field, field_validator

from app.core.phone import display_phone
from app.models.crm import (
    ActivityType,
    ActorType,
    ConsentStatus,
    QualificationStatus,
    StageCategory,
    TaskStatus,
)
from app.schemas.common import APIModel

_TAG = r"^[a-z0-9][a-z0-9_\-:]{0,63}$"


def _norm_tags(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    cleaned = sorted({t.strip().lower() for t in tags if t.strip()})
    if len(cleaned) > 30:
        raise ValueError("At most 30 tags")
    return cleaned


class StageOut(APIModel):
    id: uuid.UUID
    key: str
    name: str
    position: int
    category: StageCategory
    is_default: bool
    color: str | None


class StageCreate(APIModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,47}$")
    name: str = Field(min_length=1, max_length=80)
    category: StageCategory = StageCategory.OPEN
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class LeadBase(APIModel):
    first_name: str = Field(min_length=1, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=64, description="Any format; normalized")
    email: EmailStr | None = None
    company: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=80)
    tags: list[str] = Field(default_factory=list)
    custom_fields: dict[str, Any] = Field(default_factory=dict)
    timezone: str | None = Field(default=None, max_length=64)
    consent_status: ConsentStatus = ConsentStatus.UNKNOWN
    do_not_call: bool = False
    next_follow_up_at: datetime | None = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str]) -> list[str]:
        return _norm_tags(v) or []

    @field_validator("custom_fields")
    @classmethod
    def _custom(cls, v: dict[str, Any]) -> dict[str, Any]:
        if len(v) > 50:
            raise ValueError("At most 50 custom fields")
        for key, val in v.items():
            if len(key) > 64:
                raise ValueError("Custom field names are limited to 64 characters")
            if isinstance(val, (dict, list)):
                raise ValueError("Custom field values must be scalars")
        return v


class LeadCreate(LeadBase):
    stage_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "first_name": "Ayesha",
                    "last_name": "Khan",
                    "phone": "+1 415 555 0100",
                    "email": "ayesha@example.com",
                    "company": "Example Co",
                    "source": "web_form",
                    "tags": ["inbound", "q4"],
                }
            ]
        }
    }


class LeadUpdate(APIModel):
    first_name: str | None = Field(default=None, min_length=1, max_length=120)
    last_name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=64)
    email: EmailStr | None = None
    company: str | None = Field(default=None, max_length=200)
    job_title: str | None = Field(default=None, max_length=200)
    source: str | None = Field(default=None, max_length=80)
    tags: list[str] | None = None
    custom_fields: dict[str, Any] | None = None
    timezone: str | None = Field(default=None, max_length=64)
    consent_status: ConsentStatus | None = None
    do_not_call: bool | None = None
    next_follow_up_at: datetime | None = None
    stage_id: uuid.UUID | None = None
    qualification_status: QualificationStatus | None = None
    owner_user_id: uuid.UUID | None = None

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: list[str] | None) -> list[str] | None:
        return _norm_tags(v)


class LeadOut(APIModel):
    id: uuid.UUID
    first_name: str
    last_name: str | None
    phone_e164: str | None
    email: str | None
    company: str | None
    job_title: str | None
    source: str | None
    tags: list[str]
    custom_fields: dict[str, Any]
    stage_id: uuid.UUID
    qualification_status: QualificationStatus
    owner_user_id: uuid.UUID | None
    consent_status: ConsentStatus
    do_not_call: bool
    timezone: str | None
    next_follow_up_at: datetime | None
    last_contacted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @computed_field(description="phone_e164 formatted for display, e.g. '+44 20 7946 0001'")
    def phone_display(self) -> str | None:
        return display_phone(self.phone_e164)


class LeadSort(StrEnum):
    CREATED_DESC = "-created_at"
    CREATED_ASC = "created_at"
    UPDATED_DESC = "-updated_at"
    NAME_ASC = "first_name"
    FOLLOW_UP_ASC = "next_follow_up_at"
    LAST_CONTACTED_DESC = "-last_contacted_at"


class BulkLeadAction(StrEnum):
    ASSIGN = "assign"
    MOVE_STAGE = "move_stage"
    ADD_TAGS = "add_tags"
    DELETE = "delete"


class BulkLeadRequest(APIModel):
    lead_ids: list[uuid.UUID] = Field(min_length=1, max_length=500)
    action: BulkLeadAction
    owner_user_id: uuid.UUID | None = None
    stage_id: uuid.UUID | None = None
    tags: list[str] | None = None


class BulkLeadResult(APIModel):
    affected: int


class ActivityOut(APIModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    type: ActivityType
    actor_type: ActorType
    actor_user_id: uuid.UUID | None
    summary: str
    body: str | None
    data: dict[str, Any]
    occurred_at: datetime


class NoteCreate(APIModel):
    body: str = Field(min_length=1, max_length=10_000)


class TaskCreate(APIModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    lead_id: uuid.UUID | None = None
    due_at: datetime | None = None
    assignee_user_id: uuid.UUID | None = None


class TaskUpdate(APIModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, max_length=10_000)
    due_at: datetime | None = None
    assignee_user_id: uuid.UUID | None = None
    status: TaskStatus | None = None


class TaskOut(APIModel):
    id: uuid.UUID
    lead_id: uuid.UUID | None
    title: str
    description: str | None
    due_at: datetime | None
    status: TaskStatus
    assignee_user_id: uuid.UUID | None
    created_by_user_id: uuid.UUID | None
    completed_at: datetime | None
    created_at: datetime


class ImportRowError(APIModel):
    row: int = Field(description="1-based data row number (header excluded)")
    errors: list[str]


class ImportReport(APIModel):
    total_rows: int
    created: int
    skipped_duplicates: int
    failed: int
    dry_run: bool
    errors: list[ImportRowError]
