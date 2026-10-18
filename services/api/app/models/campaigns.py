"""Campaign domain: outbound calling campaigns and their durable per-lead work items.

A campaign snapshots its audience into `campaign_leads` when it starts; the worker
dispatches due rows through the call core and reconciles finished calls back onto
them. State changes go through `transition()` so every allowed move is explicit.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.exceptions import InvalidStateTransitionError
from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from app.models.identity import str_enum

__all__ = [
    "ACTIVE_STATUSES",
    "DEFAULT_CALLING_HOURS",
    "DEFAULT_RETRY_POLICY",
    "TERMINAL_STATUSES",
    "TRANSITIONS",
    "WEEKDAYS",
    "Campaign",
    "CampaignLead",
    "CampaignLeadStatus",
    "CampaignMode",
    "CampaignStatus",
    "transition",
]


class CampaignMode(StrEnum):
    DRY_RUN = "dry_run"  # simulated calls only; nothing leaves the platform
    LIVE = "live"  # real calls; needs ATLAS_LIVE_CALLS_ENABLED and campaigns:launch_live


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"


class CampaignLeadStatus(StrEnum):
    PENDING = "pending"  # waiting for a dispatch slot (or for next_attempt_at)
    CALLING = "calling"  # a call record exists and has not reached a terminal state
    COMPLETED = "completed"  # a call was answered and finished
    SKIPPED = "skipped"  # never called; see skip_reason
    FAILED = "failed"  # last attempt failed with a non-retriable result
    EXHAUSTED = "exhausted"  # retriable results, but the retry budget is used up


TRANSITIONS: dict[CampaignStatus, frozenset[CampaignStatus]] = {
    CampaignStatus.DRAFT: frozenset(
        {CampaignStatus.SCHEDULED, CampaignStatus.RUNNING, CampaignStatus.CANCELED}
    ),
    CampaignStatus.SCHEDULED: frozenset(
        {
            CampaignStatus.RUNNING,
            CampaignStatus.PAUSED,
            CampaignStatus.CANCELED,
            CampaignStatus.FAILED,
        }
    ),
    CampaignStatus.RUNNING: frozenset(
        {
            CampaignStatus.PAUSED,
            CampaignStatus.COMPLETED,
            CampaignStatus.CANCELED,
            CampaignStatus.FAILED,
        }
    ),
    CampaignStatus.PAUSED: frozenset(
        {
            CampaignStatus.SCHEDULED,
            CampaignStatus.RUNNING,
            CampaignStatus.CANCELED,
            CampaignStatus.FAILED,
        }
    ),
    CampaignStatus.COMPLETED: frozenset(),
    CampaignStatus.CANCELED: frozenset(),
    CampaignStatus.FAILED: frozenset(),
}
TERMINAL_STATUSES = frozenset(
    {CampaignStatus.COMPLETED, CampaignStatus.CANCELED, CampaignStatus.FAILED}
)
# Statuses the worker looks at.
ACTIVE_STATUSES = frozenset({CampaignStatus.SCHEDULED, CampaignStatus.RUNNING})

WEEKDAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_CALLING_HOURS: dict[str, list[list[str]]] = {
    "mon": [["09:00", "18:00"]],
    "tue": [["09:00", "18:00"]],
    "wed": [["09:00", "18:00"]],
    "thu": [["09:00", "18:00"]],
    "fri": [["09:00", "18:00"]],
    "sat": [],
    "sun": [],
}
DEFAULT_RETRY_POLICY: dict[str, Any] = {
    "max_attempts": 2,
    "retry_on": ["no_answer", "busy"],
    "backoff_minutes": 30,
}


class Campaign(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "campaigns"
    __table_args__ = (
        Index("ix_campaigns_org_status", "organization_id", "status"),
        Index("ix_campaigns_status_start", "status", "start_at"),
        CheckConstraint("max_concurrency BETWEEN 1 AND 50", name="max_concurrency_range"),
        CheckConstraint("calls_per_minute BETWEEN 1 AND 60", name="calls_per_minute_range"),
        CheckConstraint("max_calls IS NULL OR max_calls >= 1", name="max_calls_positive"),
    )

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    objective: Mapped[str | None] = mapped_column(Text)
    # NO ACTION (not RESTRICT) so an organization cascade can remove agents and campaigns
    # in one statement; agents are archived rather than deleted in normal operation.
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id"), nullable=False
    )
    mode: Mapped[CampaignMode] = mapped_column(
        str_enum(CampaignMode, "campaign_mode"), nullable=False, default=CampaignMode.DRY_RUN
    )
    status: Mapped[CampaignStatus] = mapped_column(
        str_enum(CampaignStatus, "campaign_status"),
        nullable=False,
        default=CampaignStatus.DRAFT,
    )
    # Audience: a saved lead filter (same keys as the leads list) and/or explicit ids.
    lead_filter: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    lead_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    # Schedule
    start_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fallback zone for leads without one; null means the organization's timezone.
    timezone: Mapped[str | None] = mapped_column(String(64))
    calling_hours: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_CALLING_HOURS)
    )
    # Pacing
    max_concurrency: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    calls_per_minute: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    retry_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: dict(DEFAULT_RETRY_POLICY)
    )
    max_calls: Mapped[int | None] = mapped_column(Integer)
    # Telephony pass-through (opaque to ATLAS; handed to the call core unchanged).
    telephony_configuration_id: Mapped[int | None] = mapped_column(Integer)
    from_phone_number_id: Mapped[int | None] = mapped_column(Integer)
    # Lifecycle
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    started_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The dispatcher leaves the campaign alone until this instant (capacity back-off).
    backoff_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(500))
    stats: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )


class CampaignLead(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """Durable work item: one row per lead per campaign (audience snapshot)."""

    __tablename__ = "campaign_leads"
    __table_args__ = (
        UniqueConstraint("campaign_id", "lead_id"),
        Index("ix_campaign_leads_due", "campaign_id", "status", "next_attempt_at"),
        Index("ix_campaign_leads_org_lead", "organization_id", "lead_id"),
    )

    campaign_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[CampaignLeadStatus] = mapped_column(
        str_enum(CampaignLeadStatus, "campaign_lead_status"),
        nullable=False,
        default=CampaignLeadStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Null means "due now" for pending rows.
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    last_outcome: Mapped[str | None] = mapped_column(String(64))
    skip_reason: Mapped[str | None] = mapped_column(String(200))


def transition(campaign: Campaign, target: CampaignStatus) -> None:
    """Move a campaign to `target` or raise InvalidStateTransitionError (HTTP 409)."""
    current = campaign.status
    if target not in TRANSITIONS[current]:
        raise InvalidStateTransitionError(
            f"A campaign that is {current.value} cannot become {target.value}",
            details={"from": current.value, "to": target.value},
        )
    campaign.status = target
