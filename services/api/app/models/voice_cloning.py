"""Cloned voices: which organization owns each voice stored on a Pocket TTS server.

The Pocket TTS server keeps cloned voices in one shared store, so ATLAS records the owner
of each one here and only ever shows an organization its own voices. The audio sample is
never stored; consent attestations are in the audit log.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, UUIDPrimaryKey

__all__ = ["CustomVoice"]


class CustomVoice(UUIDPrimaryKey, TenantScoped, Base):
    __tablename__ = "custom_voices"
    __table_args__ = (
        # Ids are random (64 bits) and global on the Pocket TTS server.
        UniqueConstraint("voice_id"),
        Index("ix_custom_voices_org_connection", "organization_id", "connection_id"),
    )

    # The voice service's connection id (Pocket TTS server) the voice was created on.
    connection_id: Mapped[str] = mapped_column(String(64), nullable=False)
    voice_id: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
