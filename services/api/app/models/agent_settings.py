"""Agent settings that live beside the versioned configuration.

- `AgentAudio`: sound files an agent's settings refer to by id (custom background noise,
  voicemail and screening recordings). ATLAS keeps the bytes so they can be previewed and
  so an older version stays restorable; each file is handed to the voice service once, on
  the first publish that uses it, and the storage reference it got is remembered.
- `AgentSecret`: write-only credentials a setting needs, encrypted at rest (the API key
  of a separate voicemail-detection model).
- `AgentWebEmbed`: the "talk to us" website widget: its public key, allowed sites and
  on/off switch. Operational, so changes apply at once without publishing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

__all__ = ["AgentAudio", "AgentSecret", "AgentWebEmbed"]


class AgentAudio(UUIDPrimaryKey, TenantScoped, Base):
    __tablename__ = "agent_audio"

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(16), nullable=False)  # ambient | message
    filename: Mapped[str] = mapped_column(String(200), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Loaded only when the file itself is needed (preview, hand-off to the voice service).
    data: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    # Where the voice service stored its copy (internal; never returned to tenants).
    runtime_storage_key: Mapped[str | None] = mapped_column(String(400))
    runtime_storage_backend: Mapped[str | None] = mapped_column(String(32))
    runtime_recording_id: Mapped[str | None] = mapped_column(String(64))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AgentSecret(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "agent_secrets"
    __table_args__ = (UniqueConstraint("agent_id", "kind"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    # The AI provider connection the secret belongs to.
    connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)


class AgentWebEmbed(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "agent_web_embeds"
    __table_args__ = (UniqueConstraint("agent_id"), UniqueConstraint("key"))

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    # Public identifier placed in the website snippet. Rotating it breaks old snippets.
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    allowed_domains: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    sessions_started: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
