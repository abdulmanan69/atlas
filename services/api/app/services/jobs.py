"""Enqueue background jobs for the ARQ worker from API and service code.

Redis is coordination, not the system of record: callers decide what to do when
the queue is unavailable (process inline, or rely on reconciliation).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from arq.connections import ArqRedis, RedisSettings, create_pool
from redis.exceptions import RedisError

from app.core.config import get_settings

log = logging.getLogger(__name__)
_pool: ArqRedis | None = None


class QueueUnavailableError(RuntimeError):
    """The job queue (Redis) could not be reached."""


def _settings() -> RedisSettings:
    settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Fail fast: callers have a degraded path and must not block a request for seconds.
    settings.conn_timeout = 2
    settings.conn_retries = 1
    return settings


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        try:
            _pool = await create_pool(_settings())
        except (RedisError, OSError) as exc:
            raise QueueUnavailableError(str(exc)) from exc
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None


async def enqueue(
    function: str,
    *args: Any,
    job_id: str | None = None,
    defer_by: float | None = None,
    **kwargs: Any,
) -> bool:
    """Queue `function` (a worker coroutine name) for execution.

    Returns False when a job with the same `job_id` is already queued (deduplicated).
    Raises QueueUnavailableError when Redis cannot be reached; the pool is reset so the
    next call reconnects.
    """
    global _pool
    try:
        pool = await get_pool()
        job = await pool.enqueue_job(
            function,
            *args,
            _job_id=job_id,
            _defer_by=timedelta(seconds=defer_by) if defer_by else None,
            **kwargs,
        )
    except (RedisError, OSError) as exc:
        _pool = None
        log.warning(
            "job_queue_unavailable", extra={"job": function, "error_type": type(exc).__name__}
        )
        raise QueueUnavailableError(str(exc)) from exc
    return job is not None
