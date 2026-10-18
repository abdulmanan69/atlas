"""Liveness, readiness and dependency health."""

from __future__ import annotations

import time
from typing import Literal

from fastapi import APIRouter, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.core.config import get_settings
from app.core.redis import get_redis
from app.db.session import get_engine
from app.workers.main import HEARTBEAT_KEY

router = APIRouter(prefix="/health", tags=["Health"])


class ComponentHealth(BaseModel):
    status: Literal["ok", "degraded", "down"]
    latency_ms: float | None = None
    detail: str | None = None


class ReadinessResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    version: str
    components: dict[str, ComponentHealth]


async def _check_db() -> ComponentHealth:
    started = time.perf_counter()
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        return ComponentHealth(status="down", detail="database unreachable")
    return ComponentHealth(status="ok", latency_ms=round((time.perf_counter() - started) * 1e3, 2))


async def _check_redis() -> ComponentHealth:
    started = time.perf_counter()
    try:
        await get_redis().ping()
    except Exception:
        return ComponentHealth(status="down", detail="redis unreachable")
    return ComponentHealth(status="ok", latency_ms=round((time.perf_counter() - started) * 1e3, 2))


async def _check_worker() -> ComponentHealth:
    """Background worker heartbeat. Informational: never fails readiness of the API."""
    try:
        beat = await get_redis().get(HEARTBEAT_KEY)
    except Exception:
        return ComponentHealth(status="down", detail="heartbeat unavailable (redis down)")
    if beat is None:
        return ComponentHealth(status="down", detail="no heartbeat in the last 2 minutes")
    age = max(0.0, time.time() - float(beat))
    return ComponentHealth(
        status="ok" if age < 90 else "degraded", detail=f"last heartbeat {int(age)}s ago"
    )


@router.get("/live", summary="Liveness probe")
async def live() -> dict[str, str]:
    """Process is up. Never checks dependencies, so it never triggers restarts on outages."""
    return {"status": "ok"}


@router.get("/ready", summary="Readiness probe", response_model=ReadinessResponse)
async def ready(response: Response) -> ReadinessResponse:
    """Database required. Redis outage is degraded, not down: rate limits fail open."""
    db, redis = await _check_db(), await _check_redis()
    overall: Literal["ok", "degraded", "down"] = "ok"
    if db.status != "ok":
        overall = "down"
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    elif redis.status != "ok":
        overall = "degraded"
    return ReadinessResponse(
        status=overall,
        version=get_settings().product_name + "/0.1.0",
        components={"database": db, "redis": redis, "worker": await _check_worker()},
    )
