"""Developer features: tenant API keys, outbound webhooks and the transactional outbox."""

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
    String,
    Text,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from app.models.identity import Role, str_enum

__all__ = ["ApiKey", "DeliveryStatus", "OutboxEvent", "WebhookDelivery", "WebhookEndpoint"]


class ApiKey(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """A long-lived credential that acts within one organization with a fixed role.

    Only the SHA-256 of the key is stored; the plaintext is shown once at creation.
    """

    __tablename__ = "api_keys"
    __table_args__ = (Index("ix_api_keys_org_created", "organization_id", "created_at"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # First characters of the key, shown in listings so a key can be recognised.
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    role: Mapped[Role] = mapped_column(str_enum(Role, "api_key_role"), nullable=False)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEndpoint(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "webhook_endpoints"

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    # Signing secret, encrypted at rest with the platform secret box.
    secret_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    # Subscribed event types; "*" subscribes to everything.
    events: Mapped[list[str]] = mapped_column(
        ARRAY(String(64)), nullable=False, default=list, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    # Consecutive failed delivery attempts; reset on the next success.
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    disabled_reason: Mapped[str | None] = mapped_column(String(200))


class DeliveryStatus(StrEnum):
    PENDING = "pending"  # not attempted yet
    DELIVERED = "delivered"  # endpoint answered 2xx
    FAILED = "failed"  # last attempt failed; another attempt is scheduled
    DEAD = "dead"  # attempts exhausted; can be retried manually


class WebhookDelivery(UUIDPrimaryKey, TenantScoped, Base):
    """One event sent to one endpoint, with its attempt history."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("ix_webhook_deliveries_due", "status", "next_attempt_at"),
        Index("ix_webhook_deliveries_endpoint_created", "endpoint_id", "created_at"),
    )

    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("webhook_endpoints.id", ondelete="CASCADE"), nullable=False
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The exact JSON document that is signed and sent (secrets redacted at emit time).
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[DeliveryStatus] = mapped_column(
        str_enum(DeliveryStatus, "webhook_delivery_status"),
        nullable=False,
        default=DeliveryStatus.PENDING,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status_code: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OutboxEvent(TenantScoped, Base):
    """Transactional outbox: written in the same transaction as the business change.

    The worker fans unprocessed rows out into `webhook_deliveries`. A sequential id
    keeps processing in commit order.
    """

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index(
            "ix_outbox_events_unprocessed",
            "id",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )
    data: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
