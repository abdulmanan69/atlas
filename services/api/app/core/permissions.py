"""Role → permission mapping. Authorization is always enforced server-side."""

from __future__ import annotations

from enum import StrEnum

from app.models.identity import Role


class Permission(StrEnum):
    ORG_READ = "org:read"
    ORG_MANAGE = "org:manage"
    MEMBERS_READ = "members:read"
    MEMBERS_MANAGE = "members:manage"
    LEADS_READ = "leads:read"
    LEADS_WRITE = "leads:write"
    LEADS_DELETE = "leads:delete"
    LEADS_IMPORT = "leads:import"
    LEADS_EXPORT = "leads:export"
    TASKS_WRITE = "tasks:write"
    AGENTS_READ = "agents:read"
    AGENTS_MANAGE = "agents:manage"
    CAMPAIGNS_READ = "campaigns:read"
    CAMPAIGNS_MANAGE = "campaigns:manage"
    CAMPAIGNS_LAUNCH_LIVE = "campaigns:launch_live"
    CALLS_READ = "calls:read"
    RECORDINGS_READ = "recordings:read"
    INTEGRATIONS_MANAGE = "integrations:manage"
    AUDIT_READ = "audit:read"
    ANALYTICS_READ = "analytics:read"


_VIEWER = {
    Permission.ORG_READ,
    Permission.LEADS_READ,
    Permission.AGENTS_READ,
    Permission.CAMPAIGNS_READ,
    Permission.CALLS_READ,
    Permission.ANALYTICS_READ,
}
_AGENT = _VIEWER | {Permission.LEADS_WRITE, Permission.TASKS_WRITE, Permission.MEMBERS_READ}
_MANAGER = _AGENT | {
    Permission.LEADS_IMPORT,
    Permission.LEADS_EXPORT,
    Permission.LEADS_DELETE,
    Permission.AGENTS_MANAGE,
    Permission.CAMPAIGNS_MANAGE,
    Permission.RECORDINGS_READ,
}
_ADMIN = _MANAGER | {
    Permission.MEMBERS_MANAGE,
    Permission.INTEGRATIONS_MANAGE,
    Permission.AUDIT_READ,
    Permission.CAMPAIGNS_LAUNCH_LIVE,
}
_OWNER = _ADMIN | {Permission.ORG_MANAGE}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: frozenset(_VIEWER),
    Role.AGENT: frozenset(_AGENT),
    Role.MANAGER: frozenset(_MANAGER),
    Role.ADMIN: frozenset(_ADMIN),
    Role.OWNER: frozenset(_OWNER),
}

# Higher rank may manage lower rank; nobody may grant a role above their own.
ROLE_RANK: dict[Role, int] = {
    Role.VIEWER: 0,
    Role.AGENT: 1,
    Role.MANAGER: 2,
    Role.ADMIN: 3,
    Role.OWNER: 4,
}


def has_permission(role: Role, permission: Permission) -> bool:
    return permission in ROLE_PERMISSIONS[role]
