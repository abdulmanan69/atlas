"""Request dependencies: DB session, authenticated user, tenant context, permissions."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import AuthenticationError, PermissionDeniedError, ValidationFailedError
from app.core.logging import api_key_id_ctx, tenant_id_ctx
from app.core.permissions import Permission, has_permission
from app.core.redis import enforce_rate_limit
from app.core.security import AccessClaims, decode_access_token
from app.db.session import get_db
from app.models.identity import (
    AuthSession,
    Membership,
    MembershipStatus,
    Organization,
    Role,
    User,
)
from app.services import api_keys as api_key_service

DB = Annotated[AsyncSession, Depends(get_db)]
_bearer = HTTPBearer(auto_error=False, description="Access token from /auth/login")
# Declared as a security scheme (not a per-operation header) so API clients and
# Swagger's "Authorize" dialog set it once for every organization-scoped call.
_org_header = APIKeyHeader(
    name="X-Organization-Id",
    scheme_name="Organization",
    description="UUID of the organization the request acts within. Verified against membership.",
    auto_error=False,
)
# Tenant API keys (created under /developer/api-keys) are an alternative to the bearer
# token for organization-scoped endpoints only; /auth/* never accepts them.
_api_key_header = APIKeyHeader(
    name="X-API-Key",
    scheme_name="ApiKey",
    description="Tenant API key. Acts within the key's organization with the key's role.",
    auto_error=False,
)
_NO_ACCESS = "You do not have access to this organization"


@dataclass(frozen=True, slots=True)
class CurrentUser:
    user: User
    session_id: uuid.UUID


@dataclass(frozen=True, slots=True)
class RequestMeta:
    ip: str | None
    user_agent: str | None


def get_request_meta(request: Request) -> RequestMeta:
    ua = request.headers.get("user-agent")
    return RequestMeta(
        ip=request.client.host if request.client else None,
        user_agent=ua[:400] if ua else None,
    )


Meta = Annotated[RequestMeta, Depends(get_request_meta)]


async def get_current_user(
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> CurrentUser:
    if creds is None or creds.scheme.lower() != "bearer":
        raise AuthenticationError("Authentication required")
    claims: AccessClaims = decode_access_token(creds.credentials)
    row = (
        await db.execute(
            select(User, AuthSession)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(User.id == claims.user_id, AuthSession.id == claims.session_id)
        )
    ).first()
    if row is None:
        raise AuthenticationError("Session not found")
    user, session = row
    now = datetime.now(UTC)
    if not user.is_active or session.revoked_at is not None or session.expires_at <= now:
        raise AuthenticationError("Session is no longer valid")
    return CurrentUser(user=user, session_id=session.id)


AuthUser = Annotated[CurrentUser, Depends(get_current_user)]


@dataclass(frozen=True, slots=True)
class TenantContext:
    """Tenant scope derived from verified membership, never from client input alone.

    For requests authenticated with a tenant API key, `user` is the member who created
    the key, `api_key_id` is set, and `membership` is an unsaved row carrying the key's
    *effective* role (the key's role capped at the creator's current role), so every
    role check (`require`, role-cap comparisons) sees the key's permissions, never the
    creator's.
    """

    user: User
    membership: Membership
    organization: Organization
    meta: RequestMeta
    api_key_id: uuid.UUID | None = None

    @property
    def org_id(self) -> uuid.UUID:
        return self.organization.id

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def role(self) -> Role:
        return self.membership.role

    @property
    def is_api_key(self) -> bool:
        return self.api_key_id is not None

    def require(self, permission: Permission) -> None:
        if not has_permission(self.membership.role, permission):
            raise PermissionDeniedError(f"Missing permission: {permission.value}")


def _parse_org_header(org_header: str) -> uuid.UUID:
    try:
        return uuid.UUID(org_header)
    except ValueError as exc:
        raise ValidationFailedError("X-Organization-Id must be a UUID") from exc


async def _tenant_from_api_key(
    db: AsyncSession, meta: RequestMeta, raw_key: str, org_header: str | None
) -> TenantContext:
    # Parse before authenticating so a malformed header fails the same way on both paths.
    requested = _parse_org_header(org_header) if org_header else None
    auth = await api_key_service.authenticate(db, raw_key)
    # The key already names its organization; a conflicting header is refused with the
    # same answer a non-member gets, so keys cannot be used to probe other tenants.
    if requested is not None and requested != auth.key.organization_id:
        raise PermissionDeniedError(_NO_ACCESS)
    await enforce_rate_limit(f"api_key:{auth.key.id}", get_settings().rate_limit_api_key_per_minute)
    await api_key_service.touch(db, auth.key)
    api_key_id_ctx.set(str(auth.key.id))
    tenant_id_ctx.set(str(auth.key.organization_id))
    # Detached, never added to the session: it only carries the effective role.
    effective = Membership(
        organization_id=auth.key.organization_id,
        user_id=auth.user.id,
        role=auth.effective_role,
        status=MembershipStatus.ACTIVE,
    )
    return TenantContext(
        user=auth.user,
        membership=effective,
        organization=auth.membership.organization,
        meta=meta,
        api_key_id=auth.key.id,
    )


async def get_tenant(
    db: DB,
    meta: Meta,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    api_key: Annotated[str | None, Depends(_api_key_header)],
    org_header: Annotated[str | None, Depends(_org_header)],
) -> TenantContext:
    if api_key:
        return await _tenant_from_api_key(db, meta, api_key, org_header)
    current = await get_current_user(db, creds)
    if not org_header:
        raise ValidationFailedError("The X-Organization-Id header is required")
    x_organization_id = _parse_org_header(org_header)
    membership = (
        await db.execute(
            select(Membership).where(
                Membership.organization_id == x_organization_id,
                Membership.user_id == current.user.id,
            )
        )
    ).scalar_one_or_none()
    # Same error for "no such org" and "not a member" to avoid tenant enumeration.
    if membership is None or membership.status != MembershipStatus.ACTIVE:
        raise PermissionDeniedError(_NO_ACCESS)
    tenant_id_ctx.set(str(membership.organization_id))
    return TenantContext(
        user=current.user,
        membership=membership,
        organization=membership.organization,
        meta=meta,
    )


Tenant = Annotated[TenantContext, Depends(get_tenant)]


def require(permission: Permission) -> Callable[[TenantContext], Awaitable[TenantContext]]:
    async def _dep(ctx: Tenant) -> TenantContext:
        ctx.require(permission)
        return ctx

    return _dep


# One typed alias per permission: `ctx: LeadsRead` = tenant context + permission check.
OrgRead = Annotated[TenantContext, Depends(require(Permission.ORG_READ))]
OrgManage = Annotated[TenantContext, Depends(require(Permission.ORG_MANAGE))]
MembersRead = Annotated[TenantContext, Depends(require(Permission.MEMBERS_READ))]
MembersManage = Annotated[TenantContext, Depends(require(Permission.MEMBERS_MANAGE))]
LeadsRead = Annotated[TenantContext, Depends(require(Permission.LEADS_READ))]
LeadsWrite = Annotated[TenantContext, Depends(require(Permission.LEADS_WRITE))]
LeadsDelete = Annotated[TenantContext, Depends(require(Permission.LEADS_DELETE))]
LeadsImport = Annotated[TenantContext, Depends(require(Permission.LEADS_IMPORT))]
LeadsExport = Annotated[TenantContext, Depends(require(Permission.LEADS_EXPORT))]
TasksWrite = Annotated[TenantContext, Depends(require(Permission.TASKS_WRITE))]
AgentsRead = Annotated[TenantContext, Depends(require(Permission.AGENTS_READ))]
AgentsManage = Annotated[TenantContext, Depends(require(Permission.AGENTS_MANAGE))]
CampaignsRead = Annotated[TenantContext, Depends(require(Permission.CAMPAIGNS_READ))]
CampaignsManage = Annotated[TenantContext, Depends(require(Permission.CAMPAIGNS_MANAGE))]
CampaignsLaunchLive = Annotated[TenantContext, Depends(require(Permission.CAMPAIGNS_LAUNCH_LIVE))]
CallsRead = Annotated[TenantContext, Depends(require(Permission.CALLS_READ))]
RecordingsRead = Annotated[TenantContext, Depends(require(Permission.RECORDINGS_READ))]
IntegrationsManage = Annotated[TenantContext, Depends(require(Permission.INTEGRATIONS_MANAGE))]
AuditRead = Annotated[TenantContext, Depends(require(Permission.AUDIT_READ))]
AnalyticsRead = Annotated[TenantContext, Depends(require(Permission.ANALYTICS_READ))]
