"""Per-organization voice runtime accounts.

Each ATLAS organization is backed by its own runtime organization (one runtime
signup each), so provider credentials, agents and call data are isolated inside
the runtime as well. Provisioning happens lazily the first time a tenant uses
a voice feature.
"""

from __future__ import annotations

import logging
import secrets
import uuid
import zlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.security import get_secret_box
from app.integrations.voice_runtime.client import (
    RuntimeUnavailableError,
    VoiceRuntimeClient,
)
from app.integrations.voice_runtime.workflow_builder import WEBHOOK_SECRET_HEADER
from app.models.voice import RuntimeAccount, RuntimeAccountStatus
from app.services import audit

log = logging.getLogger(__name__)


def _lock_key(org_id: uuid.UUID) -> int:
    # Advisory locks take a signed 64-bit key; a stable hash of the org id suffices.
    return zlib.crc32(b"runtime-account:" + org_id.bytes)


async def _provision(db: AsyncSession, org_id: uuid.UUID) -> RuntimeAccount:
    settings = get_settings()
    box = get_secret_box()
    # Random suffix: a failed earlier attempt may have left a runtime user behind.
    email = f"org-{org_id.hex}-{secrets.token_hex(3)}@{settings.voice_runtime_tenant_email_domain}"
    base = settings.voice_runtime_base_url

    async with VoiceRuntimeClient(base, timeout=settings.voice_runtime_timeout_seconds) as anon:
        token = await anon.signup(email, secrets.token_urlsafe(32), "Platform tenant")
    async with VoiceRuntimeClient(
        base, bearer=token, timeout=settings.voice_runtime_timeout_seconds
    ) as user_client:
        api_key = await user_client.create_api_key("platform")

    async with VoiceRuntimeClient(
        base, api_key=api_key, timeout=settings.voice_runtime_timeout_seconds
    ) as client:
        # The runtime bootstraps every new organization with the vendor's hosted
        # models as its default. They stay hidden and unusable from ATLAS (publishing
        # requires the tenant's own models) and are archived once the tenant saves a
        # model selection. See ai_settings.save_selection.
        bootstrap_connections = await client.list_connections()
        bootstrap_config = await client.get_default_configuration()
        bootstrap_connection = bootstrap_connections[0].uuid if bootstrap_connections else None
        hosted_trial_uuid = (
            bootstrap_connection if settings.voice_allow_hosted_trial_models else None
        )

        webhook_secret = secrets.token_urlsafe(32)
        credential_uuid = await client.create_header_credential(
            name="platform-call-events", header=WEBHOOK_SECRET_HEADER, value=webhook_secret
        )

    account = RuntimeAccount(
        organization_id=org_id,
        runtime_user_email=email,
        api_key_encrypted=box.encrypt(api_key),
        webhook_secret_encrypted=box.encrypt(webhook_secret),
        runtime_credential_uuid=credential_uuid,
        hosted_trial_connection_uuid=hosted_trial_uuid,
        bootstrap_connection_uuid=bootstrap_connection,
        bootstrap_model_config_uuid=bootstrap_config,
        status=RuntimeAccountStatus.ACTIVE,
    )
    db.add(account)
    await db.flush()
    await audit.record(
        db,
        action="voice.account_provisioned",
        organization_id=org_id,
        actor_user_id=None,
        resource_type="runtime_account",
        resource_id=account.id,
    )
    log.info("runtime_account_provisioned", extra={"organization_id": str(org_id)})
    return account


async def ensure_account(db: AsyncSession, org_id: uuid.UUID) -> RuntimeAccount:
    existing = (
        await db.execute(select(RuntimeAccount).where(RuntimeAccount.organization_id == org_id))
    ).scalar_one_or_none()
    if existing and existing.status == RuntimeAccountStatus.ACTIVE and existing.api_key_encrypted:
        return existing
    # Serialize provisioning per organization across API processes.
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lock_key(org_id)})
    existing = (
        await db.execute(select(RuntimeAccount).where(RuntimeAccount.organization_id == org_id))
    ).scalar_one_or_none()
    if existing and existing.status == RuntimeAccountStatus.ACTIVE and existing.api_key_encrypted:
        return existing
    if existing is not None:
        await db.delete(existing)
        await db.flush()
    return await _provision(db, org_id)


@asynccontextmanager
async def runtime_client(db: AsyncSession, org_id: uuid.UUID) -> AsyncIterator[VoiceRuntimeClient]:
    """A runtime client authenticated as this organization's runtime account."""
    settings = get_settings()
    account = await ensure_account(db, org_id)
    if not account.api_key_encrypted:
        raise RuntimeUnavailableError("Voice features are not set up for this organization.")
    api_key = get_secret_box().decrypt(account.api_key_encrypted)
    async with VoiceRuntimeClient(
        settings.voice_runtime_base_url,
        api_key=api_key,
        timeout=settings.voice_runtime_timeout_seconds,
    ) as client:
        yield client
