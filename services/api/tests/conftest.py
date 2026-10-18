"""Test fixtures. Tests run against a real PostgreSQL database (atlas_test) and Redis db 15."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

from cryptography.fernet import Fernet

os.environ.update(
    {
        "ATLAS_ENVIRONMENT": "test",
        "ATLAS_DATABASE_URL": os.environ.get(
            "ATLAS_TEST_DATABASE_URL",
            "postgresql+asyncpg://atlas:atlas@127.0.0.1:5433/atlas_test",
        ),
        "ATLAS_REDIS_URL": os.environ.get("ATLAS_TEST_REDIS_URL", "redis://127.0.0.1:6380/15"),
        "ATLAS_RATE_LIMIT_AUTH_PER_MINUTE": "10000",
        "ATLAS_SECRETS_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ATLAS_LOG_LEVEL": "WARNING",
        "ATLAS_LIVE_CALLS_ENABLED": "false",
        "ATLAS_VOICE_RUNTIME_BASE_URL": "http://runtime.test",
        "ATLAS_VOICE_RUNTIME_PUBLIC_URL": "http://localhost:8000",
        "ATLAS_VOICE_RUNTIME_CALLBACK_URL": "http://host.docker.internal:8080",
        "ATLAS_WEB_APP_ORIGIN": "http://localhost:5173",
        "ATLAS_VOICE_ALLOW_HOSTED_TRIAL_MODELS": "false",
    }
)

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.redis import get_redis
from app.db.base import Base
from app.db.session import get_engine
from app.main import app

API_ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "correct-horse-battery-staple"


@pytest.fixture(scope="session", autouse=True)
async def _schema() -> None:
    """Rebuild the test schema from scratch each session.

    Default: run the migrations (what CI and production use). With
    ATLAS_TEST_SCHEMA=models the schema is built straight from the SQLAlchemy models so
    work-in-progress models can be tested before their migration exists (ADR 0004).
    """
    async with get_engine().begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        await conn.run_sync(Base.metadata.drop_all)
        await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
        if os.environ.get("ATLAS_TEST_SCHEMA") == "models":
            await conn.run_sync(Base.metadata.create_all)
            return
    cfg = Config(str(API_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(API_ROOT / "migrations"))
    # Alembic's async env calls asyncio.run(), so it must not run on the test loop.
    await asyncio.to_thread(command.upgrade, cfg, "head")


@pytest.fixture(autouse=True)
async def _clean() -> AsyncIterator[None]:
    yield
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with get_engine().begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await get_redis().flushdb()


@pytest.fixture
async def client() -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


@dataclass
class Account:
    token: str
    user_id: str
    org_id: str
    email: str

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}", "X-Organization-Id": self.org_id}


async def signup(client: httpx.AsyncClient, email: str, org: str = "Acme Realty") -> Account:
    r = await client.post(
        "/api/v1/auth/signup",
        json={
            "email": email,
            "password": PASSWORD,
            "full_name": "Test User",
            "organization_name": org,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return Account(
        token=body["access_token"],
        user_id=body["user"]["id"],
        org_id=body["memberships"][0]["organization"]["id"],
        email=email,
    )


async def invite_and_join(
    client: httpx.AsyncClient, owner: Account, email: str, role: str
) -> Account:
    r = await client.post(
        "/api/v1/invitations", headers=owner.headers, json={"email": email, "role": role}
    )
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    fresh = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
    async with fresh:
        r = await fresh.post(
            "/api/v1/auth/invitations/accept",
            json={"token": token, "full_name": role.title(), "password": PASSWORD},
        )
    assert r.status_code == 200, r.text
    body = r.json()
    return Account(
        token=body["access_token"], user_id=body["user"]["id"], org_id=owner.org_id, email=email
    )


@pytest.fixture
async def owner(client: httpx.AsyncClient) -> Account:
    return await signup(client, "owner@example.com")
