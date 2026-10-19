"""Per-request database timing for the `Server-Timing` response header.

`RequestContextMiddleware` opens a `RequestTimings` for each HTTP request; SQLAlchemy
cursor events on the application engine add every statement's wall time to it. The
contextvar reaches the sync event hooks because SQLAlchemy's greenlet bridge runs them in
the request task's context.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import Engine


@dataclass(slots=True)
class RequestTimings:
    started: float = field(default_factory=time.perf_counter)
    db_seconds: float = 0.0
    db_queries: int = 0

    def header(self) -> bytes:
        total_ms = (time.perf_counter() - self.started) * 1000
        db_ms = self.db_seconds * 1000
        return (
            f'app;dur={total_ms:.1f}, db;dur={db_ms:.1f};desc="{self.db_queries} queries"'
        ).encode()


request_timings_ctx: ContextVar[RequestTimings | None] = ContextVar("request_timings", default=None)
_STARTED_KEY = "atlas_query_started"


def _before(conn: Any, _cursor: Any, _stmt: Any, _params: Any, _ctx: Any, _many: bool) -> None:
    if request_timings_ctx.get() is not None:
        conn.info.setdefault(_STARTED_KEY, []).append(time.perf_counter())


def _after(conn: Any, _cursor: Any, _stmt: Any, _params: Any, _ctx: Any, _many: bool) -> None:
    timings = request_timings_ctx.get()
    stack = conn.info.get(_STARTED_KEY)
    if timings is None or not stack:
        return
    timings.db_seconds += time.perf_counter() - stack.pop()
    timings.db_queries += 1


def _error(ctx: Any) -> None:
    # A failed statement never reaches after_cursor_execute: drop its start mark.
    conn = ctx.connection
    if conn is not None and conn.info.get(_STARTED_KEY):
        conn.info[_STARTED_KEY].pop()


def instrument_engine(engine: Engine) -> None:
    """Attach the timing hooks to a (sync) engine once."""
    if not event.contains(engine, "before_cursor_execute", _before):
        event.listen(engine, "before_cursor_execute", _before)
        event.listen(engine, "after_cursor_execute", _after)
        event.listen(engine, "handle_error", _error)
