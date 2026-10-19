"""Authentication flows: signup, login, lockout, refresh rotation and reuse detection."""

from __future__ import annotations

import httpx
import pytest

from app.core.config import get_settings
from tests.conftest import PASSWORD, Account, signup

CSRF = {"X-Requested-With": "atlas-web"}


async def test_health_endpoints(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/v1/health/live")).json() == {"status": "ok"}
    ready = (await client.get("/api/v1/health/ready")).json()
    assert ready["status"] == "ok"
    assert set(ready["components"]) == {"database", "redis", "worker"}


async def test_signup_creates_org_with_default_pipeline(
    client: httpx.AsyncClient, owner: Account
) -> None:
    me = (await client.get("/api/v1/auth/me", headers=owner.headers)).json()
    assert me["memberships"][0]["role"] == "owner"
    stages = (await client.get("/api/v1/pipeline-stages", headers=owner.headers)).json()
    assert [s["key"] for s in stages][:4] == ["new", "contacted", "qualified", "converted"]
    assert sum(s["is_default"] for s in stages) == 1


async def test_duplicate_signup_conflicts_case_insensitively(client: httpx.AsyncClient) -> None:
    await signup(client, "dup@example.com")
    r = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": "DUP@example.com",
            "password": PASSWORD,
            "full_name": "X",
            "organization_name": "Other",
        },
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "conflict"


async def test_auth_options_follow_signup_setting(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert (await client.get("/api/v1/auth/options")).json() == {"signup_enabled": True}
    monkeypatch.setattr(get_settings(), "allow_signup", False)
    assert (await client.get("/api/v1/auth/options")).json() == {"signup_enabled": False}
    r = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": "closed@example.com",
            "password": PASSWORD,
            "full_name": "X",
            "organization_name": "Closed",
        },
    )
    assert r.status_code == 403


async def test_weak_password_rejected(client: httpx.AsyncClient) -> None:
    r = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": "a@example.com",
            "password": "short",
            "full_name": "A",
            "organization_name": "Org",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_failed"


async def test_login_and_generic_error_messages(client: httpx.AsyncClient, owner: Account) -> None:
    ok = await client.post("/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD})
    assert ok.status_code == 200
    wrong = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": "wrong-password-123"}
    )
    unknown = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": PASSWORD}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["error"]["message"] == unknown.json()["error"]["message"]


async def test_account_locks_after_repeated_failures(
    client: httpx.AsyncClient, owner: Account
) -> None:
    for _ in range(5):
        await client.post(
            "/api/v1/auth/login", json={"email": owner.email, "password": "wrong-password-123"}
        )
    r = await client.post("/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD})
    assert r.status_code == 401


async def test_protected_route_requires_token(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
    r = await client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


async def test_refresh_rotation_and_reuse_detection(client: httpx.AsyncClient) -> None:
    await signup(client, "rotate@example.com")
    first_cookie = client.cookies.get("atlas_refresh")
    assert first_cookie

    missing_csrf = await client.post("/api/v1/auth/refresh")
    assert missing_csrf.status_code == 401

    r = await client.post("/api/v1/auth/refresh", headers=CSRF)
    assert r.status_code == 200
    second_cookie = client.cookies.get("atlas_refresh")
    assert second_cookie and second_cookie != first_cookie

    # Replaying the rotated token is treated as theft: the whole family is revoked.
    client.cookies.set("atlas_refresh", first_cookie, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/refresh", headers=CSRF)).status_code == 401
    client.cookies.set("atlas_refresh", second_cookie, path="/api/v1/auth")
    assert (await client.post("/api/v1/auth/refresh", headers=CSRF)).status_code == 401


async def test_logout_revokes_access_token_session(client: httpx.AsyncClient) -> None:
    acct = await signup(client, "bye@example.com")
    r = await client.post("/api/v1/auth/logout", headers={**acct.headers, **CSRF})
    assert r.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=acct.headers)).status_code == 401


async def test_sessions_list_and_revoke(client: httpx.AsyncClient, owner: Account) -> None:
    sessions = (await client.get("/api/v1/auth/sessions", headers=owner.headers)).json()
    assert len(sessions) == 1 and sessions[0]["current"] is True
    r = await client.delete(f"/api/v1/auth/sessions/{sessions[0]['id']}", headers=owner.headers)
    assert r.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=owner.headers)).status_code == 401


async def test_errors_carry_request_id(client: httpx.AsyncClient) -> None:
    r = await client.get("/api/v1/auth/me", headers={"X-Request-ID": "trace-abc-12345"})
    assert r.headers["x-request-id"] == "trace-abc-12345"
    assert r.json()["error"]["request_id"] == "trace-abc-12345"
    assert r.headers["x-content-type-options"] == "nosniff"
