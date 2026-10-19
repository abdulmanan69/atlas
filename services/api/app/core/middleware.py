"""Pure-ASGI middleware: correlation IDs, access logs, Server-Timing, body size limit,
security headers, compression exclusions."""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.logging import api_key_id_ctx, request_id_ctx, tenant_id_ctx
from app.core.timing import RequestTimings, request_timings_ctx

log = logging.getLogger("atlas.access")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._\-]{8,64}$")

SECURITY_HEADERS: list[tuple[bytes, bytes]] = [
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), geolocation=(), microphone=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp, *, hsts: bool = False) -> None:
        self.app = app
        self.hsts = hsts

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        rid_token = request_id_ctx.set(request_id)
        tid_token = tenant_id_ctx.set(None)
        kid_token = api_key_id_ctx.set(None)
        timings = RequestTimings()
        timing_token = request_timings_ctx.set(timings)
        started = time.perf_counter()
        status_holder = {"code": 500}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode()))
                headers.append((b"server-timing", timings.header()))
                headers.extend(SECURITY_HEADERS)
                if self.hsts:
                    headers.append(
                        (b"strict-transport-security", b"max-age=63072000; includeSubDomains")
                    )
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if not path.startswith("/api/v1/health"):
                log.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "path": path,
                        "status": status_holder["code"],
                        "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    },
                )
            request_id_ctx.reset(rid_token)
            tenant_id_ctx.reset(tid_token)
            api_key_id_ctx.reset(kid_token)
            request_timings_ctx.reset(timing_token)


class BodySizeLimitMiddleware:
    """Reject request bodies above a configured byte limit (413).

    `path_limits` raises the limit for specific routes (pattern on the path), e.g. sound
    file uploads, which carry a 10 MB file plus the multipart envelope.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_bytes: int,
        path_limits: tuple[tuple[re.Pattern[str], int], ...] = (),
    ) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.path_limits = path_limits

    def _limit(self, path: str) -> int:
        for pattern, limit in self.path_limits:
            if pattern.fullmatch(path):
                return limit
        return self.max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        max_bytes = self._limit(scope.get("path", ""))
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_bytes:
            await _reject_413(send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > max_bytes:
                    raise _PayloadTooLargeError
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _PayloadTooLargeError:
            await _reject_413(send)


class _PayloadTooLargeError(Exception):
    pass


async def _reject_413(send: Send) -> None:
    body = b'{"error":{"code":"payload_too_large","message":"Request body too large"}}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": body})


class SkipCompressedMediaMiddleware:
    """Mark audio/video/image responses as already encoded so GZip leaves them alone.

    Sits inside `GZipMiddleware`: recordings are streamed (often with Range-friendly
    Content-Length) and are already compressed, so gzipping them only burns CPU.
    """

    _PREFIXES = (b"audio/", b"video/", b"image/")

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                names = {k.lower(): v for k, v in headers}
                if b"content-encoding" not in names and names.get(b"content-type", b"").startswith(
                    self._PREFIXES
                ):
                    headers.append((b"content-encoding", b"identity"))
                    message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class EmbedCorsMiddleware:
    """Cross-origin access for the public website-widget routes.

    Widgets run on tenants' own sites, so these routes cannot use the API's fixed CORS
    allow-list. Preflights are answered here (before `CORSMiddleware` would refuse an
    unknown origin) and responses echo the caller's origin without credentials. Whether a
    site may actually start a session is decided by the route, per widget, from its
    allowed sites; nothing here grants access to anything else.
    """

    def __init__(self, app: ASGIApp, *, prefix: str) -> None:
        self.app = app
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.prefix):
            await self.app(scope, receive, send)
            return
        headers = dict(scope["headers"])
        origin = headers.get(b"origin")
        if origin is None:
            await self.app(scope, receive, send)
            return
        if scope["method"] == "OPTIONS" and b"access-control-request-method" in headers:
            await send(
                {
                    "type": "http.response.start",
                    "status": 204,
                    "headers": [
                        (b"access-control-allow-origin", origin),
                        (b"vary", b"Origin"),
                        (b"access-control-allow-methods", b"GET, POST, OPTIONS"),
                        (b"access-control-allow-headers", b"Content-Type"),
                        (b"access-control-max-age", b"600"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b""})
            return

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                kept: list[tuple[bytes, bytes]] = []
                vary: list[bytes] = []
                for k, v in message.get("headers", []):
                    name = k.lower()
                    if name == b"vary":
                        vary.extend(p.strip() for p in v.split(b",") if p.strip())
                    elif name not in (
                        b"access-control-allow-origin",
                        b"access-control-allow-credentials",
                    ):
                        kept.append((k, v))
                if b"origin" not in {p.lower() for p in vary}:
                    vary.append(b"Origin")
                message["headers"] = [
                    *kept,
                    (b"access-control-allow-origin", origin),
                    (b"vary", b", ".join(vary)),
                ]
            await send(message)

        await self.app(scope, receive, send_wrapper)
