"""Tenant API keys: creation, revocation and request authentication.

A key is `atlas_<random>`; only its SHA-256 is stored. A key acts within the
organization it was created in, with the role chosen at creation, capped at the
creator's role. The cap is enforced live: if the creator's role is reduced the key is
reduced with it, and if the creator leaves the organization the key stops working.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import Select, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import (
    AuthenticationError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.permissions import ROLE_RANK
from app.core.security import constant_time_equals, hash_token
from app.models.developer import ApiKey
from app.models.identity import Membership, MembershipStatus, Role, User
from app.schemas.developer import ApiKeyCreate
from app.services import audit

if TYPE_CHECKING:
    from app.api.v1.deps import TenantContext

KEY_PREFIX = "atlas_"
# "atlas_" plus the first 8 random characters: enough to recognise a key in a list.
KEY_PREFIX_SHOWN_CHARS = len(KEY_PREFIX) + 8
MAX_ACTIVE_KEYS = 100
LAST_USED_GRANULARITY = timedelta(minutes=1)


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def shown_prefix(key: str) -> str:
    return key[:KEY_PREFIX_SHOWN_CHARS]


def hash_key(key: str) -> str:
    return hash_token(key)


@dataclass(frozen=True, slots=True)
class AuthenticatedKey:
    key: ApiKey
    user: User
    membership: Membership
    effective_role: Role


async def authenticate(db: AsyncSession, raw_key: str) -> AuthenticatedKey:
    """Resolve a presented key to its organization and effective role, or raise 401."""
    if not raw_key.startswith(KEY_PREFIX) or not 20 <= len(raw_key) <= 128:
        raise AuthenticationError("Invalid API key")
    digest = hash_key(raw_key)
    row = (
        await db.execute(
            select(ApiKey, User, Membership)
            .join(User, User.id == ApiKey.created_by_user_id)
            .join(
                Membership,
                (Membership.user_id == User.id)
                & (Membership.organization_id == ApiKey.organization_id),
            )
            .where(ApiKey.key_hash == digest)
        )
    ).first()
    if row is None:
        raise AuthenticationError("Invalid API key")
    key, user, membership = row
    if not constant_time_equals(key.key_hash, digest):
        raise AuthenticationError("Invalid API key")
    now = datetime.now(UTC)
    if key.revoked_at is not None:
        raise AuthenticationError("API key has been revoked")
    if key.expires_at is not None and key.expires_at <= now:
        raise AuthenticationError("API key has expired")
    if not user.is_active or membership.status != MembershipStatus.ACTIVE:
        raise AuthenticationError("API key is no longer valid: its creator is not an active member")
    effective = key.role if ROLE_RANK[key.role] <= ROLE_RANK[membership.role] else membership.role
    return AuthenticatedKey(key=key, user=user, membership=membership, effective_role=effective)


async def touch(db: AsyncSession, key: ApiKey, now: datetime | None = None) -> None:
    """Record use, at most once per minute so hot keys do not write on every request."""
    now = now or datetime.now(UTC)
    if key.last_used_at is not None and now - key.last_used_at < LAST_USED_GRANULARITY:
        return
    await db.execute(
        update(ApiKey)
        .where(ApiKey.id == key.id)
        .values(last_used_at=now)
        .execution_options(synchronize_session=False)
    )


def _live(org_id: uuid.UUID) -> Select[tuple[ApiKey]]:
    return select(ApiKey).where(ApiKey.organization_id == org_id)


async def list_api_keys(
    db: AsyncSession, ctx: TenantContext, *, include_revoked: bool = False
) -> list[ApiKey]:
    stmt = _live(ctx.org_id)
    if not include_revoked:
        stmt = stmt.where(ApiKey.revoked_at.is_(None))
    rows = await db.execute(stmt.order_by(ApiKey.created_at.desc(), ApiKey.id))
    return list(rows.scalars().all())


async def get_api_key(db: AsyncSession, ctx: TenantContext, key_id: uuid.UUID) -> ApiKey:
    key = (await db.execute(_live(ctx.org_id).where(ApiKey.id == key_id))).scalar_one_or_none()
    if key is None:
        raise NotFoundError("API key not found")
    return key


async def create_api_key(
    db: AsyncSession, ctx: TenantContext, data: ApiKeyCreate
) -> tuple[ApiKey, str]:
    """Create a key and return it with the plaintext, which is never stored."""
    if ROLE_RANK[data.role] > ROLE_RANK[ctx.role]:
        raise PermissionDeniedError("You cannot create a key with a role above your own")
    now = datetime.now(UTC)
    if data.expires_at is not None and data.expires_at <= now:
        raise ValidationFailedError(
            "expires_at must be in the future", details={"field": "expires_at"}
        )
    active = int(
        await db.scalar(
            select(func.count())
            .select_from(ApiKey)
            .where(ApiKey.organization_id == ctx.org_id, ApiKey.revoked_at.is_(None))
        )
        or 0
    )
    if active >= MAX_ACTIVE_KEYS:
        raise ValidationFailedError(
            f"An organization may have at most {MAX_ACTIVE_KEYS} active API keys"
        )
    raw = generate_key()
    key = ApiKey(
        organization_id=ctx.org_id,
        name=data.name.strip(),
        key_prefix=shown_prefix(raw),
        key_hash=hash_key(raw),
        role=data.role,
        created_by_user_id=ctx.user_id,
        expires_at=data.expires_at,
    )
    db.add(key)
    await db.flush()
    await audit.record(
        db,
        action="api_key.created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="api_key",
        resource_id=key.id,
        changes={
            "name": key.name,
            "role": key.role.value,
            "key_prefix": key.key_prefix,
            "expires_at": key.expires_at.isoformat() if key.expires_at else None,
        },
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(key)
    return key, raw


async def revoke_api_key(db: AsyncSession, ctx: TenantContext, key_id: uuid.UUID) -> ApiKey:
    """Revocation is immediate and permanent; revoking twice is a no-op."""
    key = await get_api_key(db, ctx, key_id)
    if key.revoked_at is not None:
        return key
    key.revoked_at = datetime.now(UTC)
    await audit.record(
        db,
        action="api_key.revoked",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="api_key",
        resource_id=key.id,
        changes={"name": key.name, "key_prefix": key.key_prefix},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    return key
