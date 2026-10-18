"""Campaign contracts: audience, schedule, pacing, retries, progress."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator

from app.models.campaigns import (
    DEFAULT_CALLING_HOURS,
    WEEKDAYS,
    CampaignLeadStatus,
    CampaignMode,
    CampaignStatus,
)
from app.models.crm import QualificationStatus
from app.schemas.common import APIModel

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_END_OF_DAY = "24:00"
RetryTrigger = Literal["no_answer", "busy", "failed"]
MAX_EXPLICIT_LEADS = 5000


def _valid_timezone(name: str | None) -> str | None:
    if name is None:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown timezone: {name}") from exc
    return name


class CallingHours(APIModel):
    """Local-time windows per weekday, e.g. {"mon": [["09:00", "18:00"]]}."""

    mon: list[tuple[str, str]] = Field(default_factory=list, max_length=4)
    tue: list[tuple[str, str]] = Field(default_factory=list, max_length=4)
    wed: list[tuple[str, str]] = Field(default_factory=list, max_length=4)
    thu: list[tuple[str, str]] = Field(default_factory=list, max_length=4)
    fri: list[tuple[str, str]] = Field(default_factory=list, max_length=4)
    sat: list[tuple[str, str]] = Field(default_factory=list, max_length=4)
    sun: list[tuple[str, str]] = Field(default_factory=list, max_length=4)

    @field_validator(*WEEKDAYS)
    @classmethod
    def _ranges(cls, v: list[tuple[str, str]]) -> list[tuple[str, str]]:
        for start, end in v:
            if not (_HHMM.match(start) and (_HHMM.match(end) or end == _END_OF_DAY)):
                raise ValueError("Calling hours must be HH:MM (24h); a window may end at 24:00")
            if start >= end:
                raise ValueError("Each calling window must start before it ends")
        return sorted(v)

    @model_validator(mode="after")
    def _some_window(self) -> CallingHours:
        if not any(getattr(self, d) for d in WEEKDAYS):
            raise ValueError("At least one calling window is required")
        return self

    @classmethod
    def default(cls) -> CallingHours:
        return cls.model_validate(DEFAULT_CALLING_HOURS)


def _default_retry_on() -> list[RetryTrigger]:
    return ["no_answer", "busy"]


class RetryPolicy(APIModel):
    max_attempts: int = Field(default=2, ge=1, le=5, description="Total attempts per lead")
    retry_on: list[RetryTrigger] = Field(default_factory=_default_retry_on)
    backoff_minutes: int = Field(
        default=30, ge=1, le=1440, description="Wait before attempt n is backoff x (n-1)"
    )

    @field_validator("retry_on")
    @classmethod
    def _dedupe(cls, v: list[RetryTrigger]) -> list[RetryTrigger]:
        return list(dict.fromkeys(v))


class LeadFilterSpec(APIModel):
    """A saved lead filter; same keys as the leads list query."""

    q: str | None = Field(default=None, max_length=200)
    stage_id: uuid.UUID | None = None
    owner_user_id: uuid.UUID | None = None
    unassigned: bool = False
    tag: str | None = Field(default=None, max_length=64)
    qualification_status: QualificationStatus | None = None
    do_not_call: bool | None = None
    source: str | None = Field(default=None, max_length=80)
    created_from: datetime | None = None
    created_to: datetime | None = None

    def is_empty(self) -> bool:
        return all(v in (None, False) for v in self.model_dump().values())


class _CampaignFields(APIModel):
    name: str = Field(min_length=1, max_length=120)
    objective: str | None = Field(default=None, max_length=2000)
    agent_id: uuid.UUID
    mode: CampaignMode = CampaignMode.DRY_RUN
    lead_filter: LeadFilterSpec | None = None
    lead_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_EXPLICIT_LEADS)
    start_at: datetime | None = Field(default=None, description="Null: start immediately")
    end_at: datetime | None = Field(default=None, description="Null: run until done")
    timezone: str | None = Field(
        default=None, max_length=64, description="Fallback for leads without a timezone"
    )
    calling_hours: CallingHours = Field(default_factory=CallingHours.default)
    max_concurrency: int = Field(default=2, ge=1, le=50)
    calls_per_minute: int = Field(default=10, ge=1, le=60)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    max_calls: int | None = Field(default=None, ge=1, description="Stop after this many calls")
    telephony_configuration_id: int | None = Field(default=None, ge=1)
    from_phone_number_id: int | None = Field(default=None, ge=1)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return _valid_timezone(v)

    @field_validator("lead_ids")
    @classmethod
    def _unique_ids(cls, v: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return None if v is None else list(dict.fromkeys(v))


class CampaignCreate(_CampaignFields):
    @model_validator(mode="after")
    def _consistent(self) -> CampaignCreate:
        has_filter = self.lead_filter is not None and not self.lead_filter.is_empty()
        if not has_filter and not self.lead_ids:
            raise ValueError("Choose an audience: a lead filter and/or explicit lead ids")
        if self.start_at and self.end_at and self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "name": "October re-engagement",
                    "objective": "Re-qualify leads that went quiet",
                    "agent_id": "00000000-0000-4000-8000-000000000000",
                    "mode": "dry_run",
                    "lead_filter": {"tag": "q4"},
                    "calling_hours": {"mon": [["09:00", "18:00"]], "tue": [["09:00", "18:00"]]},
                    "max_concurrency": 2,
                    "calls_per_minute": 10,
                    "retry_policy": {
                        "max_attempts": 2,
                        "retry_on": ["no_answer", "busy"],
                        "backoff_minutes": 30,
                    },
                }
            ]
        }
    }


class CampaignUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    objective: str | None = Field(default=None, max_length=2000)
    agent_id: uuid.UUID | None = None
    mode: CampaignMode | None = None
    lead_filter: LeadFilterSpec | None = None
    lead_ids: list[uuid.UUID] | None = Field(default=None, max_length=MAX_EXPLICIT_LEADS)
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=64)
    calling_hours: CallingHours | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=50)
    calls_per_minute: int | None = Field(default=None, ge=1, le=60)
    retry_policy: RetryPolicy | None = None
    max_calls: int | None = Field(default=None, ge=1)
    telephony_configuration_id: int | None = Field(default=None, ge=1)
    from_phone_number_id: int | None = Field(default=None, ge=1)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        return _valid_timezone(v)

    @field_validator("lead_ids")
    @classmethod
    def _unique_ids(cls, v: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        return None if v is None else list(dict.fromkeys(v))


class CampaignOut(APIModel):
    id: uuid.UUID
    name: str
    objective: str | None
    agent_id: uuid.UUID
    mode: CampaignMode
    status: CampaignStatus
    lead_filter: LeadFilterSpec | None
    lead_ids: list[uuid.UUID] | None
    start_at: datetime | None
    end_at: datetime | None
    timezone: str | None
    calling_hours: CallingHours
    max_concurrency: int
    calls_per_minute: int
    retry_policy: RetryPolicy
    max_calls: int | None
    telephony_configuration_id: int | None
    from_phone_number_id: int | None
    created_by_user_id: uuid.UUID | None
    started_by_user_id: uuid.UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    backoff_until: datetime | None
    last_error: str | None
    stats: dict[str, Any] = Field(description="Cached progress counters, refreshed by the worker")
    created_at: datetime
    updated_at: datetime


class CampaignPreview(APIModel):
    """What starting the campaign now would do. No side effects."""

    matched: int = Field(description="Leads matching the audience definition")
    eligible: int = Field(description="Leads that would be called")
    skipped: dict[str, int] = Field(description="Leads that would not be called, by reason")
    capped_by_max_calls: bool
    estimated_duration_seconds: int | None
    calling_window_open: bool = Field(description="In the campaign's fallback timezone, now")
    next_window_opens_at: datetime | None


class LeadStatusCounts(APIModel):
    pending: int = 0
    calling: int = 0
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    exhausted: int = 0

    @property
    def total(self) -> int:
        return sum(self.model_dump().values())


class CampaignProgress(APIModel):
    status: CampaignStatus
    leads: LeadStatusCounts
    calls_total: int
    calls_by_state: dict[str, int]
    calls_by_outcome: dict[str, int]
    answer_rate: float | None = Field(description="Answered ÷ finished calls")
    average_duration_seconds: int | None
    started_at: datetime | None
    finished_at: datetime | None
    backoff_until: datetime | None
    eta_seconds: int | None = Field(description="Rough time to drain pending leads")


class CampaignLeadPerson(APIModel):
    id: uuid.UUID
    first_name: str
    last_name: str | None
    phone_e164: str | None
    company: str | None
    timezone: str | None


class CampaignLeadOut(APIModel):
    id: uuid.UUID
    lead_id: uuid.UUID
    status: CampaignLeadStatus
    attempts: int
    next_attempt_at: datetime | None
    last_call_id: uuid.UUID | None
    last_outcome: str | None
    skip_reason: str | None
    updated_at: datetime
    lead: CampaignLeadPerson | None
