"""Organizations, memberships and invitations."""

from __future__ import annotations

import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import TenantContext
from app.core.exceptions import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationFailedError,
)
from app.core.permissions import ROLE_RANK
from app.core.security import hash_password, hash_token, new_opaque_token
from app.models.crm import DEFAULT_STAGES, PipelineStage
from app.models.identity import Invitation, Membership, MembershipStatus, Organization, Role, User
from app.schemas.identity import (
    AcceptInvitationRequest,
    InvitationCreate,
    MemberUpdate,
    OrganizationCreate,
    OrganizationUpdate,
)
from app.services import audit

INVITATION_TTL = timedelta(days=7)


def _slugify(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60] or "org"
    return f"{base}-{secrets.token_hex(3)}"


async def create_organization(
    db: AsyncSession, owner: User, data: OrganizationCreate
) -> Organization:
    org = Organization(
        name=data.name,
        slug=_slugify(data.name),
        timezone=data.timezone,
        default_country=data.default_country,
        settings={},
    )
    db.add(org)
    await db.flush()
    db.add(Membership(organization_id=org.id, user_id=owner.id, role=Role.OWNER))
    for position, (key, name, category) in enumerate(DEFAULT_STAGES):
        db.add(
            PipelineStage(
                organization_id=org.id,
                key=key,
                name=name,
                position=position,
                category=category,
                is_default=(position == 0),
            )
        )
    await audit.record(
        db,
        action="organization.created",
        organization_id=org.id,
        actor_user_id=owner.id,
        resource_type="organization",
        resource_id=org.id,
    )
    await db.flush()
    return org


async def list_memberships(db: AsyncSession, user_id: uuid.UUID) -> list[Membership]:
    rows = await db.execute(
        select(Membership)
        .where(Membership.user_id == user_id, Membership.status == MembershipStatus.ACTIVE)
        .order_by(Membership.created_at)
    )
    return list(rows.scalars().all())


async def update_organization(
    db: AsyncSession, ctx: TenantContext, data: OrganizationUpdate
) -> Organization:
    org = ctx.organization
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(org, field, value)
    await audit.record(
        db,
        action="organization.updated",
        organization_id=org.id,
        actor_user_id=ctx.user_id,
        resource_type="organization",
        resource_id=org.id,
        changes=changes,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    return org


async def list_members(db: AsyncSession, ctx: TenantContext) -> list[Membership]:
    rows = await db.execute(
        select(Membership)
        .where(Membership.organization_id == ctx.org_id)
        .order_by(Membership.created_at)
    )
    return list(rows.scalars().all())


async def _get_member(db: AsyncSession, ctx: TenantContext, member_id: uuid.UUID) -> Membership:
    member = (
        await db.execute(
            select(Membership).where(
                Membership.id == member_id, Membership.organization_id == ctx.org_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise NotFoundError("Member not found")
    return member


async def _active_owner_count(db: AsyncSession, org_id: uuid.UUID) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(
                Membership.organization_id == org_id,
                Membership.role == Role.OWNER,
                Membership.status == MembershipStatus.ACTIVE,
            )
        )
        or 0
    )


def _assert_can_manage(actor: Membership, target_role: Role) -> None:
    if ROLE_RANK[target_role] > ROLE_RANK[actor.role]:
        raise PermissionDeniedError("You cannot manage a role above your own")


async def update_member(
    db: AsyncSession, ctx: TenantContext, member_id: uuid.UUID, data: MemberUpdate
) -> Membership:
    member = await _get_member(db, ctx, member_id)
    _assert_can_manage(ctx.membership, member.role)
    if data.role is not None:
        _assert_can_manage(ctx.membership, data.role)
    demoting_owner = member.role == Role.OWNER and (
        (data.role is not None and data.role != Role.OWNER)
        or data.status == MembershipStatus.SUSPENDED
    )
    if demoting_owner and await _active_owner_count(db, ctx.org_id) <= 1:
        raise ValidationFailedError("An organization must keep at least one active owner")
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    for field, value in changes.items():
        setattr(member, field, value)
    await audit.record(
        db,
        action="member.updated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="membership",
        resource_id=member.id,
        changes=changes,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    return member


async def remove_member(db: AsyncSession, ctx: TenantContext, member_id: uuid.UUID) -> None:
    member = await _get_member(db, ctx, member_id)
    _assert_can_manage(ctx.membership, member.role)
    if member.role == Role.OWNER and await _active_owner_count(db, ctx.org_id) <= 1:
        raise ValidationFailedError("An organization must keep at least one active owner")
    await db.delete(member)
    await audit.record(
        db,
        action="member.removed",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="membership",
        resource_id=member_id,
        changes={"user_id": str(member.user_id)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )


async def create_invitation(
    db: AsyncSession, ctx: TenantContext, data: InvitationCreate
) -> tuple[Invitation, str]:
    _assert_can_manage(ctx.membership, data.role)
    email = data.email.lower()
    already_member = await db.scalar(
        select(Membership.id)
        .join(User, User.id == Membership.user_id)
        .where(Membership.organization_id == ctx.org_id, func.lower(User.email) == email)
    )
    if already_member:
        raise ConflictError("That user is already a member")
    pending = await db.scalar(
        select(Invitation.id).where(
            Invitation.organization_id == ctx.org_id,
            func.lower(Invitation.email) == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
    )
    if pending:
        raise ConflictError("A pending invitation already exists for that email")
    token = new_opaque_token()
    invitation = Invitation(
        organization_id=ctx.org_id,
        email=email,
        role=data.role,
        token_hash=hash_token(token),
        invited_by_user_id=ctx.user_id,
        expires_at=datetime.now(UTC) + INVITATION_TTL,
    )
    db.add(invitation)
    await db.flush()
    await audit.record(
        db,
        action="invitation.created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="invitation",
        resource_id=invitation.id,
        changes={"email": email, "role": data.role.value},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return invitation, token


async def list_invitations(db: AsyncSession, ctx: TenantContext) -> list[Invitation]:
    rows = await db.execute(
        select(Invitation)
        .where(Invitation.organization_id == ctx.org_id)
        .order_by(Invitation.created_at.desc())
    )
    return list(rows.scalars().all())


async def revoke_invitation(db: AsyncSession, ctx: TenantContext, invitation_id: uuid.UUID) -> None:
    inv = (
        await db.execute(
            select(Invitation).where(
                Invitation.id == invitation_id, Invitation.organization_id == ctx.org_id
            )
        )
    ).scalar_one_or_none()
    if inv is None:
        raise NotFoundError("Invitation not found")
    if inv.accepted_at is None and inv.revoked_at is None:
        inv.revoked_at = datetime.now(UTC)
        await audit.record(
            db,
            action="invitation.revoked",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="invitation",
            resource_id=inv.id,
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )


async def accept_invitation(
    db: AsyncSession, data: AcceptInvitationRequest, current_user: User | None
) -> User:
    """Accept as the signed-in user, or create an account for a new invitee."""
    inv = (
        await db.execute(
            select(Invitation)
            .where(Invitation.token_hash == hash_token(data.token))
            .with_for_update()
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if inv is None or inv.revoked_at or inv.accepted_at or inv.expires_at <= now:
        raise ValidationFailedError("Invitation is invalid or has expired")

    user = current_user
    if user is not None:
        if user.email.lower() != inv.email.lower():
            raise PermissionDeniedError("This invitation was issued to a different email")
    else:
        existing = (
            await db.execute(select(User).where(func.lower(User.email) == inv.email.lower()))
        ).scalar_one_or_none()
        if existing is not None:
            raise ConflictError("An account exists for this email. Sign in to accept.")
        if not data.password or not data.full_name:
            raise ValidationFailedError("full_name and password are required for new accounts")
        user = User(
            email=inv.email, full_name=data.full_name, password_hash=hash_password(data.password)
        )
        db.add(user)
        await db.flush()

    db.add(Membership(organization_id=inv.organization_id, user_id=user.id, role=inv.role))
    inv.accepted_at = now
    await audit.record(
        db,
        action="invitation.accepted",
        organization_id=inv.organization_id,
        actor_user_id=user.id,
        resource_type="invitation",
        resource_id=inv.id,
    )
    await db.flush()
    return user
