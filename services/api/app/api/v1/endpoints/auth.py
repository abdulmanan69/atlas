"""Authentication: sign-up, login, refresh rotation, logout, sessions, invitations."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.api.v1.deps import DB, AuthUser, Meta, get_current_user
from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.redis import enforce_rate_limit
from app.schemas.common import ERROR_RESPONSES
from app.schemas.identity import (
    AcceptInvitationRequest,
    LoginRequest,
    MembershipOut,
    MeResponse,
    SessionOut,
    SignupRequest,
    TokenResponse,
    UserOut,
)
from app.services import auth as auth_service
from app.services import organizations as org_service

router = APIRouter(prefix="/auth", tags=["Authentication"], responses=ERROR_RESPONSES)
_REFRESH_PATH = "/api/v1/auth"
CSRF_HEADER = "X-Requested-With"


def _set_refresh_cookie(response: Response, token: str) -> None:
    s = get_settings()
    response.set_cookie(
        s.refresh_cookie_name,
        token,
        max_age=s.refresh_token_ttl_seconds,
        httponly=True,
        secure=s.cookie_secure,
        samesite="strict",
        path=_REFRESH_PATH,
    )


def _require_csrf_header(x_requested_with: str | None) -> None:
    # A custom header cannot be sent cross-site without a CORS preflight we do not allow.
    if not x_requested_with:
        raise AuthenticationError(f"Missing {CSRF_HEADER} header")


async def _token_response(db: DB, issued: auth_service.IssuedTokens) -> TokenResponse:
    memberships = await org_service.list_memberships(db, issued.user.id)
    return TokenResponse(
        access_token=issued.access_token,
        expires_in=issued.expires_in,
        user=UserOut.model_validate(issued.user),
        memberships=[MembershipOut.model_validate(m) for m in memberships],
    )


@router.post(
    "/signup",
    response_model=TokenResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account and its first organization",
)
async def signup(data: SignupRequest, response: Response, db: DB, meta: Meta) -> TokenResponse:
    await enforce_rate_limit(f"signup:{meta.ip}", get_settings().rate_limit_auth_per_minute)
    issued = await auth_service.signup(db, data, meta)
    _set_refresh_cookie(response, issued.refresh_token)
    return await _token_response(db, issued)


@router.post("/login", response_model=TokenResponse, summary="Sign in with email and password")
async def login(data: LoginRequest, response: Response, db: DB, meta: Meta) -> TokenResponse:
    limit = get_settings().rate_limit_auth_per_minute
    await enforce_rate_limit(f"login-ip:{meta.ip}", limit)
    await enforce_rate_limit(f"login-email:{data.email.lower()}", limit)
    issued = await auth_service.login(db, data, meta)
    _set_refresh_cookie(response, issued.refresh_token)
    return await _token_response(db, issued)


@router.post(
    "/refresh",
    response_model=TokenResponse,
    summary="Rotate the refresh cookie and obtain a new access token",
)
async def refresh(
    response: Response,
    db: DB,
    meta: Meta,
    x_requested_with: Annotated[str | None, Header()] = None,
    atlas_refresh: Annotated[str | None, Cookie()] = None,
) -> TokenResponse:
    _require_csrf_header(x_requested_with)
    if not atlas_refresh:
        raise AuthenticationError("No refresh session")
    issued = await auth_service.refresh(db, atlas_refresh, meta)
    _set_refresh_cookie(response, issued.refresh_token)
    return await _token_response(db, issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="End the current session")
async def logout(
    response: Response,
    db: DB,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
    x_requested_with: Annotated[str | None, Header()] = None,
    atlas_refresh: Annotated[str | None, Cookie()] = None,
) -> None:
    _require_csrf_header(x_requested_with)
    session_id: uuid.UUID | None = None
    if creds is not None:
        try:
            session_id = (await get_current_user(db, creds)).session_id
        except AuthenticationError:
            session_id = None
    await auth_service.logout(db, atlas_refresh, session_id)
    response.delete_cookie(get_settings().refresh_cookie_name, path=_REFRESH_PATH)


@router.get("/me", response_model=MeResponse, summary="Current user and organization memberships")
async def me(current: AuthUser, db: DB) -> MeResponse:
    memberships = await org_service.list_memberships(db, current.user.id)
    return MeResponse(
        user=UserOut.model_validate(current.user),
        memberships=[MembershipOut.model_validate(m) for m in memberships],
    )


@router.get("/sessions", response_model=list[SessionOut], summary="List active sessions")
async def sessions(current: AuthUser, db: DB) -> list[SessionOut]:
    rows = await auth_service.list_sessions(db, current.user.id)
    return [
        SessionOut(
            id=s.id,
            created_at=s.created_at,
            expires_at=s.expires_at,
            user_agent=s.user_agent,
            ip_address=str(s.ip_address) if s.ip_address else None,
            current=s.id == current.session_id,
        )
        for s in rows
    ]


@router.delete(
    "/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke one of your sessions",
)
async def revoke_session(session_id: uuid.UUID, current: AuthUser, db: DB) -> None:
    await auth_service.revoke_session(db, current.user.id, session_id)


@router.post(
    "/invitations/accept", response_model=TokenResponse, summary="Accept an organization invitation"
)
async def accept_invitation(
    data: AcceptInvitationRequest,
    response: Response,
    db: DB,
    meta: Meta,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(HTTPBearer(auto_error=False))],
) -> TokenResponse:
    await enforce_rate_limit(f"invite:{meta.ip}", get_settings().rate_limit_auth_per_minute)
    current = await get_current_user(db, creds) if creds else None
    user = await org_service.accept_invitation(db, data, current.user if current else None)
    issued = await auth_service._issue(db, user, meta)
    _set_refresh_cookie(response, issued.refresh_token)
    return await _token_response(db, issued)
