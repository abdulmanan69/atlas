"""ASGI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import install_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.core.redis import close_redis
from app.db.session import dispose_engine
from app.services import jobs

API_DESCRIPTION = """
Multi-tenant platform API for CRM, AI voice agents, campaigns and call analytics.

**Authentication** — obtain an access token from `POST /api/v1/auth/login` and send it as
`Authorization: Bearer <token>`. Tokens expire after 15 minutes; rotate them with
`POST /api/v1/auth/refresh` (refresh cookie + `X-Requested-With` header).

**Tenancy** — every organization-scoped endpoint requires `X-Organization-Id`. Access is
verified against your membership on every request.

**Errors** — all errors use `{"error": {"code", "message", "request_id", "details"}}`.
Quote `request_id` (also returned in the `X-Request-ID` header) when reporting issues.

**Pagination** — list endpoints accept `page` and `page_size` and return `total`.

**Rate limits** — authentication endpoints are limited per IP and per account; exceeding the
limit returns `429` with code `rate_limited`.

**Versioning** — breaking changes ship under a new prefix (`/api/v2`). Deprecated endpoints
are announced with a `Deprecation` header at least 90 days before removal.
"""


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    await jobs.close_pool()
    await dispose_engine()
    await close_redis()


def create_app() -> FastAPI:
    settings = get_settings()
    settings.validate_for_runtime()
    configure_logging(settings.log_level, settings.service_name)
    docs = settings.docs_enabled

    app = FastAPI(
        title=f"{settings.product_name} Platform API",
        version="0.1.0",
        description=API_DESCRIPTION,
        lifespan=lifespan,
        docs_url="/docs" if docs else None,
        redoc_url="/redoc" if docs else None,
        openapi_url="/openapi.json" if docs else None,
        swagger_ui_parameters={"persistAuthorization": True, "displayRequestDuration": True},
    )
    install_exception_handlers(app)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "X-Organization-Id",
            "X-Requested-With",
            "X-Request-ID",
            "Idempotency-Key",
        ],
        expose_headers=["X-Request-ID"],
        max_age=600,
    )
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(RequestContextMiddleware, hsts=settings.is_production)
    app.include_router(api_router)
    return app


app = create_app()
