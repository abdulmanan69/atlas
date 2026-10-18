"""Voice domain: per-tenant runtime accounts, AI agents and their immutable versions."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TenantScoped, Timestamps, UUIDPrimaryKey
from app.models.identity import str_enum


class RuntimeAccountStatus(StrEnum):
    ACTIVE = "active"
    FAILED = "failed"


class RuntimeAccount(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    """The voice-runtime organization backing one ATLAS organization.

    Each tenant gets its own runtime organization and API key, so provider
    credentials, agents and call records are isolated inside the runtime too.
    """

    __tablename__ = "runtime_accounts"
    __table_args__ = (UniqueConstraint("organization_id"),)

    runtime_user_email: Mapped[str] = mapped_column(String(320), nullable=False)
    api_key_encrypted: Mapped[str | None] = mapped_column(Text)
    # Shared secret the runtime sends with call events; verified on every webhook.
    webhook_secret_encrypted: Mapped[str | None] = mapped_column(Text)
    runtime_credential_uuid: Mapped[str | None] = mapped_column(String(64))
    # The model configuration ATLAS manages as this tenant's default.
    managed_model_config_uuid: Mapped[str | None] = mapped_column(String(64))
    # Present only when the deployment allows the runtime's hosted trial models.
    hosted_trial_connection_uuid: Mapped[str | None] = mapped_column(String(64))
    # Models the runtime creates for every new organization (vendor-hosted). Hidden from
    # tenants and archived once the tenant saves its own models; the runtime refuses to
    # archive the active default, so this cannot happen at provisioning time.
    bootstrap_connection_uuid: Mapped[str | None] = mapped_column(String(64))
    bootstrap_model_config_uuid: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[RuntimeAccountStatus] = mapped_column(
        str_enum(RuntimeAccountStatus, "runtime_account_status"),
        nullable=False,
        default=RuntimeAccountStatus.ACTIVE,
    )
    last_error: Mapped[str | None] = mapped_column(String(500))


class AgentDirection(StrEnum):
    OUTBOUND = "outbound"
    INBOUND = "inbound"


class AgentStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class SyncStatus(StrEnum):
    NOT_SYNCED = "not_synced"
    SYNCED = "synced"
    ERROR = "error"


class Agent(UUIDPrimaryKey, Timestamps, TenantScoped, Base):
    __tablename__ = "agents"
    __table_args__ = (Index("ix_agents_org_status", "organization_id", "status"),)

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    direction: Mapped[AgentDirection] = mapped_column(
        str_enum(AgentDirection, "agent_direction"), nullable=False
    )
    status: Mapped[AgentStatus] = mapped_column(
        str_enum(AgentStatus, "agent_status"), nullable=False, default=AgentStatus.DRAFT
    )
    # Latest saved version, and the version that live calls use (null until published).
    latest_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    published_version: Mapped[int | None] = mapped_column(Integer)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )

    # Runtime mapping. Internal: never returned to tenants except as sync status.
    runtime_workflow_id: Mapped[str | None] = mapped_column(String(64))
    runtime_workflow_uuid: Mapped[str | None] = mapped_column(String(64))
    sync_status: Mapped[SyncStatus] = mapped_column(
        str_enum(SyncStatus, "sync_status"), nullable=False, default=SyncStatus.NOT_SYNCED
    )
    sync_error: Mapped[str | None] = mapped_column(String(500))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentVersion(UUIDPrimaryKey, TenantScoped, Base):
    """Immutable snapshot of an agent's configuration. Edits create a new version."""

    __tablename__ = "agent_versions"
    __table_args__ = (UniqueConstraint("agent_id", "version"),)

    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    note: Mapped[str | None] = mapped_column(String(300))
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
