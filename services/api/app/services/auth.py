"""Sign-up, login with lockout, refresh-token rotation with reuse detection, logout."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import RequestMeta
from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, ConflictError, PermissionDeniedError
from app.core.security import (
    create_access_token,
    hash_password,
    hash_token,
    new_opaque_token,
    password_needs_rehash,
    verify_password,
)
from app.models.identity import AuthSession, User
from app.schemas.identity import LoginRequest, OrganizationCreate, SignupRequest
from app.services import audit, organizations

log = logging.getLogger(__name__)

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)


@dataclass(frozen=True, slots=True)
class IssuedTokens:
    user: User
    access_token: str
    refresh_token: str
    expires_in: int


async def _issue(
    db: AsyncSession, user: User, meta: RequestMeta, family_id: uuid.UUID | None = None
) -> IssuedTokens:
    settings = get_settings()
    refresh = new_opaque_token()
    session = AuthSession(
        user_id=user.id,
        family_id=family_id or uuid.uuid4(),
        refresh_token_hash=hash_token(refresh),
        expires_at=datetime.now(UTC) + timedelta(seconds=settings.refresh_token_ttl_seconds),
        user_agent=meta.user_agent,
        ip_address=meta.ip,
    )
    db.add(session)
    await db.flush()
    return IssuedTokens(
        user=user,
        access_token=create_access_token(user.id, session.id),
        refresh_token=refresh,
        expires_in=settings.access_token_ttl_seconds,
    )


async def signup(db: AsyncSession, data: SignupRequest, meta: RequestMeta) -> IssuedTokens:
    if not get_settings().allow_signup:
        raise PermissionDeniedError("Self-service sign-up is disabled for this deployment")
    email = data.email.lower()
    exists = await db.scalar(select(User.id).where(func.lower(User.email) == email))
    if exists:
        raise ConflictError("An account with this email already exists")
    user = User(email=email, full_name=data.full_name, password_hash=hash_password(data.password))
    db.add(user)
    await db.flush()
    await organizations.create_organization(
        db, user, OrganizationCreate(name=data.organization_name)
    )
    await audit.record(
        db,
        action="user.signed_up",
        organization_id=None,
        actor_user_id=user.id,
        resource_type="user",
        resource_id=user.id,
        ip=meta.ip,
        user_agent=meta.user_agent,
    )
    return await _issue(db, user, meta)


async def login(db: AsyncSession, data: LoginRequest, meta: RequestMeta) -> IssuedTokens:
    user = (
        await db.execute(select(User).where(func.lower(User.email) == data.email.lower()))
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if user is not None and user.locked_until and user.locked_until > now:
        # Still verify to keep timing uniform, but refuse regardless of the result.
        verify_password(data.password, user.password_hash)
        raise AuthenticationError("Invalid email or password")

    if not verify_password(data.password, user.password_hash if user else None) or not user:
        if user is not None:
            user.failed_login_count += 1
            if user.failed_login_count >= MAX_FAILED_LOGINS:
                user.locked_until = now + LOCKOUT
                user.failed_login_count = 0
                log.warning("account_locked", extra={"user_id": str(user.id)})
        await audit.record(
            db,
            action="auth.login_failed",
            organization_id=None,
            actor_user_id=user.id if user else None,
            ip=meta.ip,
            user_agent=meta.user_agent,
        )
        # Commit the failure counter even though the request fails.
        await db.commit()
        raise AuthenticationError("Invalid email or password")

    if not user.is_active:
        raise AuthenticationError("Invalid email or password")
    user.failed_login_count = 0
    user.locked_until = None
    user.last_login_at = now
    if user.password_hash and password_needs_rehash(user.password_hash):
        user.password_hash = hash_password(data.password)
    await audit.record(
        db,
        action="auth.login",
        organization_id=None,
        actor_user_id=user.id,
        ip=meta.ip,
        user_agent=meta.user_agent,
    )
    return await _issue(db, user, meta)


async def refresh(db: AsyncSession, refresh_token: str, meta: RequestMeta) -> IssuedTokens:
    """Rotate a refresh token. Presenting an already-rotated token revokes the whole family."""
    session = (
        await db.execute(
            select(AuthSession)
            .where(AuthSession.refresh_token_hash == hash_token(refresh_token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if session is None:
        raise AuthenticationError("Invalid refresh token")
    if session.rotated_at is not None or session.revoked_at is not None:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.family_id == session.family_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
        await audit.record(
            db,
            action="auth.refresh_reuse_detected",
            organization_id=None,
            actor_user_id=session.user_id,
            ip=meta.ip,
            user_agent=meta.user_agent,
        )
        await db.commit()
        raise AuthenticationError("Refresh token is no longer valid")
    if session.expires_at <= now:
        raise AuthenticationError("Refresh token expired")
    user = await db.get(User, session.user_id)
    if user is None or not user.is_active:
        raise AuthenticationError("Account is disabled")
    session.rotated_at = now
    session.revoked_at = now
    return await _issue(db, user, meta, family_id=session.family_id)


async def logout(db: AsyncSession, refresh_token: str | None, session_id: uuid.UUID | None) -> None:
    now = datetime.now(UTC)
    if session_id is not None:
        await db.execute(
            update(AuthSession)
            .where(AuthSession.id == session_id, AuthSession.revoked_at.is_(None))
            .values(revoked_at=now)
        )
    if refresh_token:
        await db.execute(
            update(AuthSession)
            .where(
                AuthSession.refresh_token_hash == hash_token(refresh_token),
                AuthSession.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )


async def list_sessions(db: AsyncSession, user_id: uuid.UUID) -> list[AuthSession]:
    rows = await db.execute(
        select(AuthSession)
        .where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at.is_(None),
            AuthSession.expires_at > datetime.now(UTC),
        )
        .order_by(AuthSession.created_at.desc())
    )
    return list(rows.scalars().all())


async def revoke_session(db: AsyncSession, user_id: uuid.UUID, session_id: uuid.UUID) -> None:
    await db.execute(
        update(AuthSession)
        .where(AuthSession.id == session_id, AuthSession.user_id == user_id)
        .values(revoked_at=datetime.now(UTC))
    )
