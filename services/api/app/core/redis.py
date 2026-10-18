"""Shared Redis client and a fixed-window rate limiter."""

from __future__ import annotations

import logging
import time

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings
from app.core.exceptions import RateLimitedError

log = logging.getLogger(__name__)
_client: Redis | None = None


def get_redis() -> Redis:
    global _client
    if _client is None:
        _client = Redis.from_url(
            get_settings().redis_url,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
            decode_responses=True,
        )
    return _client


async def close_redis() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None


async def enforce_rate_limit(bucket: str, limit: int, window_seconds: int = 60) -> None:
    """Raise RateLimitedError when `bucket` exceeds `limit` hits per window.

    Fails open if Redis is unreachable: availability of login beats strict
    limiting, and the account lockout in the auth service still applies.
    """
    window = int(time.time() // window_seconds)
    key = f"rl:{bucket}:{window}"
    try:
        redis = get_redis()
        async with redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.expire(key, window_seconds + 1)
            count, _ = await pipe.execute()
    except RedisError:
        log.warning("rate_limit_unavailable", extra={"bucket": bucket.split(":")[0]})
        return
    if int(count) > limit:
        raise RateLimitedError(
            "Too many requests. Try again shortly.", details={"retry_after": window_seconds}
        )
