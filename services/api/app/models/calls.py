"""Call records: one row per attempted conversation between an AI agent and a person.

ATLAS is the system of record. The voice runtime's identifiers are kept in
adapter-owned columns and never exposed to tenants.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from app.models.identity import str_enum

__all__ = ["Call", "CallDirection", "CallEvent", "CallMode", "CallState"]


class CallDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"  # also browser test calls: the person calls in to the agent


class CallMode(StrEnum):
    LIVE = "live"
    SIMULATED = "simulated"  # dry runs: no phone call is placed, outcome is synthetic
    # Browser test call from the agent editor: real conversation with the published
    # agent over the microphone, no phone call. Never counted in analytics, campaign
    # stats or lead history, and never sent to outbound webhooks.
    TEST = "test"


class CallState(StrEnum):
    QUEUED = "queued"
    DIALING = "dialing"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    NO_ANSWER = "no_answer"
    BUSY = "busy"
    FAILED = "failed"
    CANCELED = "canceled"


TERMINAL_STATES = frozenset(
    {CallState.COMPLETED, CallState.NO_ANSWER, CallState.BUSY, CallState.FAILED, CallState.CANCELED}
)


class Call(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "calls"
    __table_args__ = (
        Index("ix_calls_org_created", "organization_id", "created_at"),
        Index("ix_calls_org_state", "organization_id", "state"),
        Index("ix_calls_org_lead_created", "organization_id", "lead_id", "created_at"),
        Index("ix_calls_org_agent", "organization_id", "agent_id"),
        Index("ix_calls_org_campaign_state", "organization_id", "campaign_id", "state"),
        Index(
            "uq_calls_runtime_run",
            "runtime_run_id",
            unique=True,
            postgresql_where=text("runtime_run_id IS NOT NULL"),
        ),
    )

    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("leads.id", ondelete="SET NULL")
    )
    agent_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="SET NULL")
    )
    agent_version: Mapped[int | None] = mapped_column(Integer)
    campaign_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    direction: Mapped[CallDirection] = mapped_column(
        str_enum(CallDirection, "call_direction"), nullable=False
    )
    mode: Mapped[CallMode] = mapped_column(str_enum(CallMode, "call_mode"), nullable=False)
    state: Mapped[CallState] = mapped_column(
        str_enum(CallState, "call_state"), nullable=False, default=CallState.QUEUED
    )
    to_phone_e164: Mapped[str | None] = mapped_column(String(20))
    from_phone_e164: Mapped[str | None] = mapped_column(String(20))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[int | None] = mapped_column(Integer)
    # Structured result
    outcome: Mapped[str | None] = mapped_column(String(64))  # agent's outcome code
    captured: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    opted_out: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    summary: Mapped[str | None] = mapped_column(Text)
    transcript: Mapped[str | None] = mapped_column(Text)
    recording_available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    usage: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    estimated_cost: Mapped[float | None] = mapped_column(Numeric(10, 4))
    # Per-turn response times parsed from the runtime's event log (ADR 0006):
    # {"summary": {...}, "turns": [...]}; null when nothing was measured.
    latency: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(String(500))
    # Runtime mapping (internal)
    runtime_workflow_id: Mapped[str | None] = mapped_column(String(64))
    runtime_run_id: Mapped[str | None] = mapped_column(String(64))
    runtime_transcript_key: Mapped[str | None] = mapped_column(String(300))
    runtime_recording_key: Mapped[str | None] = mapped_column(String(300))
    result_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    initiated_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )


class CallEvent(UUIDPrimaryKey, TenantScoped, Base):
    """Append-only history of a call: state changes, results, artifacts, lead matching.

    Rows are never updated or deleted by application code; they explain how a call
    record reached its current shape (webhook, reconciliation, simulation, ...).
    """

    __tablename__ = "call_events"
    __table_args__ = (Index("ix_call_events_call_seq", "call_id", "seq"),)

    call_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="CASCADE"), nullable=False
    )
    # Insertion order: clocks tie within a transaction, a sequence never does.
    seq: Mapped[int] = mapped_column(BigInteger, Identity(), nullable=False, unique=True)
    type: Mapped[str] = mapped_column(String(48), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
