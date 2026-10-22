"""AI servers: self-hosted model hosts an organization registered once (ADR 0007).

A models host publishes its language model, Whisper, Pocket TTS and live speech-to-text
through one AI gateway address with a token. ATLAS checks the gateway's /health, creates
the voice service's model connections for it, and keeps their ids here so the token can
be rotated in one place and the server cannot be removed while it is in use. The token
and the live speech-to-text secret are encrypted at rest and never returned.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey

__all__ = ["AiServer"]


class AiServer(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "ai_servers"
    __table_args__ = (UniqueConstraint("organization_id", "base_url"),)

    name: Mapped[str] = mapped_column(String(80), nullable=False)
    # The gateway's address without a trailing slash, e.g. https://ai.example.com.
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    live_stt_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    # ok | degraded | down | unchecked
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="unchecked", server_default="unchecked"
    )
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_latency_ms: Mapped[int | None] = mapped_column(Integer)
    # Per service, as the gateway reported it (up, state, latency, version, models); no
    # addresses or secrets.
    last_health: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    # Why the last check failed, in plain words (never upstream text).
    last_error: Mapped[str | None] = mapped_column(String(300))
    # The voice service's connections ATLAS created for this server.
    llm_connection_id: Mapped[str | None] = mapped_column(String(64))
    whisper_connection_id: Mapped[str | None] = mapped_column(String(64))
    tts_connection_id: Mapped[str | None] = mapped_column(String(64))
    voice_connection_id: Mapped[str | None] = mapped_column(String(64))
    live_stt_connection_id: Mapped[str | None] = mapped_column(String(64))
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
