"""Auth, organization, membership and invitation contracts."""

from __future__ import annotations

import uuid
from datetime import datetime
from zoneinfo import available_timezones

from pydantic import EmailStr, Field, field_validator

from app.models.identity import MembershipStatus, Role
from app.schemas.common import APIModel

_TIMEZONES = available_timezones()


def _check_tz(value: str) -> str:
    if value not in _TIMEZONES:
        raise ValueError("Unknown IANA timezone")
    return value


class Password(APIModel):
    password: str = Field(min_length=12, max_length=128)


class SignupRequest(Password):
    email: EmailStr
    full_name: str = Field(min_length=1, max_length=200)
    organization_name: str = Field(min_length=2, max_length=200)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "email": "owner@example.com",
                    "password": "a-long-unique-passphrase",
                    "full_name": "Sara Ahmed",
                    "organization_name": "Example Realty",
                }
            ]
        }
    }


class AuthOptions(APIModel):
    signup_enabled: bool = Field(
        description="Whether anyone can create a new organization from the sign-up page"
    )


class LoginRequest(APIModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(APIModel):
    id: uuid.UUID
    email: str
    full_name: str
    created_at: datetime


class OrganizationOut(APIModel):
    id: uuid.UUID
    name: str
    slug: str
    timezone: str
    default_country: str
    created_at: datetime


class MembershipOut(APIModel):
    id: uuid.UUID
    role: Role
    status: MembershipStatus
    organization: OrganizationOut


class TokenResponse(APIModel):
    access_token: str
    token_type: str = "bearer"  # noqa: S105
    expires_in: int
    user: UserOut
    memberships: list[MembershipOut]


class MeResponse(APIModel):
    user: UserOut
    memberships: list[MembershipOut]


class OrganizationCreate(APIModel):
    name: str = Field(min_length=2, max_length=200)
    timezone: str = "UTC"
    default_country: str = Field(default="US", pattern=r"^[A-Z]{2}$")

    _tz = field_validator("timezone")(_check_tz)


class OrganizationUpdate(APIModel):
    name: str | None = Field(default=None, min_length=2, max_length=200)
    timezone: str | None = None
    default_country: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str | None) -> str | None:
        return None if value is None else _check_tz(value)


class MemberOut(APIModel):
    id: uuid.UUID
    role: Role
    status: MembershipStatus
    created_at: datetime
    user: UserOut


class MemberUpdate(APIModel):
    role: Role | None = None
    status: MembershipStatus | None = None


class InvitationCreate(APIModel):
    email: EmailStr
    role: Role = Role.AGENT


class InvitationOut(APIModel):
    id: uuid.UUID
    email: str
    role: Role
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class InvitationCreated(InvitationOut):
    token: str = Field(description="Shown once. Deliver to the invitee out of band.")


class AcceptInvitationRequest(APIModel):
    token: str = Field(min_length=20, max_length=200)
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    password: str | None = Field(default=None, min_length=12, max_length=128)


class SessionOut(APIModel):
    id: uuid.UUID
    created_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip_address: str | None
    current: bool = False
