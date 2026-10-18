"""Inbound webhook deliveries: one row per delivery id, so retries are idempotent."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, UUIDPrimaryKey
from app.models.identity import str_enum

__all__ = ["WebhookEvent", "WebhookEventStatus"]


class WebhookEventStatus(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    FAILED = "failed"


class WebhookEvent(UUIDPrimaryKey, TenantScoped, Base):
    """A delivery accepted from an external system (today: the voice service).

    The stored payload is redacted and bounded; it is kept for support, never read
    back as a source of truth (results are always re-fetched from the provider).
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        UniqueConstraint("provider", "delivery_id"),
        Index("ix_webhook_events_org_received", "organization_id", "received_at"),
    )

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    delivery_id: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    status: Mapped[WebhookEventStatus] = mapped_column(
        str_enum(WebhookEventStatus, "webhook_event_status"),
        nullable=False,
        default=WebhookEventStatus.RECEIVED,
    )
    error: Mapped[str | None] = mapped_column(String(500))
    call_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("calls.id", ondelete="SET NULL")
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
