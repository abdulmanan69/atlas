"""ARQ worker entry point: `arq app.workers.main.WorkerSettings`.

Long-running and scheduled work runs here, never inside HTTP requests. Job modules
under `app/workers/` are discovered automatically: a module exposes `FUNCTIONS`
(list of coroutines) and/or `CRON_JOBS` (list of `arq.cron(...)`).

Every job coroutine takes the ARQ context dict first: `async def job(ctx, *args)`.
Jobs must be idempotent: ARQ may retry them after a crash.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
import time
from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.core.redis import close_redis, get_redis
from app.db.session import dispose_engine

log = logging.getLogger("atlas.worker")
HEARTBEAT_KEY = "atlas:worker:heartbeat"
HEARTBEAT_TTL_SECONDS = 120


async def heartbeat(ctx: dict[Any, Any], *args: Any, **kwargs: Any) -> None:
    await get_redis().set(HEARTBEAT_KEY, str(int(time.time())), ex=HEARTBEAT_TTL_SECONDS)


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings.log_level, "atlas-worker")
    await heartbeat(ctx)
    log.info("worker_started", extra={"functions": [f.__name__ for f in _functions]})


async def shutdown(_: dict[str, Any]) -> None:
    await dispose_engine()
    await close_redis()
    log.info("worker_stopped")


def _discover() -> tuple[list[Any], list[Any]]:
    functions: list[Any] = [heartbeat]
    crons: list[Any] = [cron(heartbeat, second={0, 30}, run_at_startup=False)]
    package = importlib.import_module("app.workers")
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module_info.name == "main":
            continue
        module = importlib.import_module(f"app.workers.{module_info.name}")
        functions.extend(getattr(module, "FUNCTIONS", []))
        crons.extend(getattr(module, "CRON_JOBS", []))
    return functions, crons


_functions, _cron_jobs = _discover()


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    functions: ClassVar[list[Any]] = _functions
    cron_jobs: ClassVar[list[Any]] = _cron_jobs
    on_startup = startup
    on_shutdown = shutdown
    max_jobs = 20
    job_timeout = 300
    keep_result = 3600
    health_check_interval = 30
