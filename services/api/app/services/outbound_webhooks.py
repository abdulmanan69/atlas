"""Outbound webhooks: endpoint management, URL policy, signing and HTTP delivery.

Delivery semantics are at-least-once: consumers must treat the event id as an
idempotency key. Endpoint URLs are checked against the shared outbound destination policy
(app.core.net_policy) both when saved and again right before every delivery attempt
(DNS answers change).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.exceptions import InvalidStateTransitionError, NotFoundError, ValidationFailedError
from app.core.logging import redact
from app.core.net_policy import resolve_host, validate_destination_url
from app.core.security import get_secret_box
from app.models.developer import DeliveryStatus, WebhookDelivery, WebhookEndpoint
from app.schemas.developer import WILDCARD_EVENT, WebhookEndpointCreate, WebhookEndpointUpdate
from app.services import audit

if TYPE_CHECKING:
    from app.api.v1.deps import TenantContext

log = logging.getLogger(__name__)

SECRET_PREFIX = "whsec_"  # noqa: S105 - a prefix, not a credential
SIGNATURE_VERSION = "v1"
USER_AGENT = "ATLAS-Webhooks/1.0"
MAX_ENDPOINTS = 20
BACKOFF_BASE_SECONDS = 60
BACKOFF_MAX_SECONDS = 2 * 60 * 60
PING_EVENT = "ping"

EVENT_CATALOG: dict[str, str] = {
    "lead.created": "A lead was created (manually, by import, or through the API).",
    "lead.updated": "Lead fields changed. `data.changed_fields` lists which.",
    "lead.stage_changed": "A lead moved to another pipeline stage.",
    "call.started": "An AI agent call started. Browser test calls are not sent.",
    "call.completed": (
        "A call ended; includes outcome and captured fields. Browser test calls are not sent."
    ),
    "campaign.started": "A campaign started dialing.",
    "campaign.completed": "A campaign finished all its leads.",
    WILDCARD_EVENT: "Every event type, including ones added later.",
}


# ------------------------------------------------------------------ URL policy

# Module-level name on purpose: tests swap the resolver here to avoid live DNS.
_resolve_host = resolve_host


async def validate_webhook_url(url: str) -> None:
    """Enforce the shared destination policy: https, no credentials, public host only."""
    await validate_destination_url(
        url,
        label="Webhook",
        allow_private=get_settings().allow_private_webhook_urls,
        resolver=_resolve_host,
    )


# ------------------------------------------------------------------- signing


def generate_secret() -> str:
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def canonical_body(payload: dict[str, Any]) -> str:
    """The exact JSON text that is signed and sent."""
    return json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False)


def sign(secret: str, timestamp: str, body: str) -> str:
    digest = hmac.new(secret.encode(), f"{timestamp}.{body}".encode(), hashlib.sha256).hexdigest()
    return f"{SIGNATURE_VERSION}={digest}"


def build_envelope(
    *,
    event_id: uuid.UUID,
    event_type: str,
    organization_id: uuid.UUID,
    created_at: datetime,
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": str(event_id),
        "type": event_type,
        "created_at": created_at.isoformat(),
        "organization_id": str(organization_id),
        "data": data,
    }


def backoff_seconds(attempt: int) -> int:
    """Exponential backoff with +/-20% jitter: ~1, 2, 4, ... minutes, capped at two hours."""
    base = min(BACKOFF_BASE_SECONDS << min(max(attempt - 1, 0), 20), BACKOFF_MAX_SECONDS)
    jitter = base * (secrets.randbelow(41) - 20) // 100
    return base + jitter


# ------------------------------------------------------------------ delivery


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    ok: bool
    status_code: int | None
    latency_ms: int
    error: str | None


def http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=get_settings().webhook_timeout_seconds,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    )


async def send_event(
    client: httpx.AsyncClient,
    *,
    url: str,
    secret: str,
    event_type: str,
    event_id: uuid.UUID,
    payload: dict[str, Any],
    delivery_id: uuid.UUID | None = None,
) -> DeliveryResult:
    """POST one signed event. Never raises; the outcome is returned."""
    try:
        await validate_webhook_url(url)
    except ValidationFailedError as exc:
        return DeliveryResult(False, None, 0, f"URL rejected by security policy: {exc.message}")
    body = canonical_body(payload)
    timestamp = str(int(time.time()))
    headers = {
        "Content-Type": "application/json",
        "X-Atlas-Event": event_type,
        "X-Atlas-Event-Id": str(event_id),
        "X-Atlas-Timestamp": timestamp,
        "X-Atlas-Signature": sign(secret, timestamp, body),
    }
    if delivery_id is not None:
        headers["X-Atlas-Delivery-Id"] = str(delivery_id)
    started = time.perf_counter()
    try:
        # Stream so an endpoint cannot make us buffer an arbitrarily large response.
        async with client.stream("POST", url, content=body.encode(), headers=headers) as response:
            status_code = response.status_code
    except Exception as exc:  # network, TLS, timeout: all count as a failed attempt
        latency = int((time.perf_counter() - started) * 1000)
        return DeliveryResult(False, None, latency, f"{type(exc).__name__}: {exc}"[:500])
    latency = int((time.perf_counter() - started) * 1000)
    ok = 200 <= status_code < 300
    error = None if ok else f"Endpoint responded with HTTP {status_code}"
    return DeliveryResult(ok, status_code, latency, error)


# ------------------------------------------------------------- endpoint CRUD


def _secret_of(endpoint: WebhookEndpoint) -> str:
    return get_secret_box().decrypt(endpoint.secret_encrypted)


async def list_endpoints(db: AsyncSession, ctx: TenantContext) -> list[WebhookEndpoint]:
    rows = await db.execute(
        select(WebhookEndpoint)
        .where(WebhookEndpoint.organization_id == ctx.org_id)
        .order_by(WebhookEndpoint.created_at.desc(), WebhookEndpoint.id)
    )
    return list(rows.scalars().all())


async def get_endpoint(
    db: AsyncSession, ctx: TenantContext, endpoint_id: uuid.UUID
) -> WebhookEndpoint:
    endpoint = (
        await db.execute(
            select(WebhookEndpoint).where(
                WebhookEndpoint.organization_id == ctx.org_id, WebhookEndpoint.id == endpoint_id
            )
        )
    ).scalar_one_or_none()
    if endpoint is None:
        raise NotFoundError("Webhook endpoint not found")
    return endpoint


async def create_endpoint(
    db: AsyncSession, ctx: TenantContext, data: WebhookEndpointCreate
) -> tuple[WebhookEndpoint, str]:
    """Create an endpoint and return it with its signing secret (shown once)."""
    count = int(
        await db.scalar(
            select(func.count())
            .select_from(WebhookEndpoint)
            .where(WebhookEndpoint.organization_id == ctx.org_id)
        )
        or 0
    )
    if count >= MAX_ENDPOINTS:
        raise ValidationFailedError(
            f"An organization may have at most {MAX_ENDPOINTS} webhook endpoints"
        )
    await validate_webhook_url(data.url)
    secret = generate_secret()
    endpoint = WebhookEndpoint(
        organization_id=ctx.org_id,
        url=data.url,
        description=data.description,
        secret_encrypted=get_secret_box().encrypt(secret),
        events=data.events,
        is_active=data.is_active,
        created_by_user_id=ctx.user_id,
    )
    db.add(endpoint)
    await db.flush()
    await audit.record(
        db,
        action="webhook.created",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        changes={"url": endpoint.url, "events": endpoint.events},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(endpoint)
    return endpoint, secret


async def update_endpoint(
    db: AsyncSession, ctx: TenantContext, endpoint_id: uuid.UUID, data: WebhookEndpointUpdate
) -> WebhookEndpoint:
    endpoint = await get_endpoint(db, ctx, endpoint_id)
    changes = data.model_dump(exclude_unset=True)
    tracked: dict[str, Any] = {}
    if "url" in changes and changes["url"] is not None and changes["url"] != endpoint.url:
        await validate_webhook_url(changes["url"])
        endpoint.url = changes["url"]
        tracked["url"] = endpoint.url
    if "description" in changes and changes["description"] != endpoint.description:
        endpoint.description = changes["description"]
        tracked["description"] = "changed"
    if (
        "events" in changes
        and changes["events"] is not None
        and changes["events"] != endpoint.events
    ):
        endpoint.events = changes["events"]
        tracked["events"] = endpoint.events
    if "is_active" in changes and changes["is_active"] is not None:
        if changes["is_active"] != endpoint.is_active:
            tracked["is_active"] = changes["is_active"]
        endpoint.is_active = changes["is_active"]
        if endpoint.is_active:
            endpoint.failure_count = 0
            endpoint.disabled_at = None
            endpoint.disabled_reason = None
    if tracked:
        await audit.record(
            db,
            action="webhook.updated",
            organization_id=ctx.org_id,
            actor_user_id=ctx.user_id,
            resource_type="webhook_endpoint",
            resource_id=endpoint.id,
            changes=tracked,
            ip=ctx.meta.ip,
            user_agent=ctx.meta.user_agent,
        )
    await db.flush()
    await db.refresh(endpoint)
    return endpoint


async def delete_endpoint(db: AsyncSession, ctx: TenantContext, endpoint_id: uuid.UUID) -> None:
    endpoint = await get_endpoint(db, ctx, endpoint_id)
    await audit.record(
        db,
        action="webhook.deleted",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        changes={"url": endpoint.url},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.delete(endpoint)
    await db.flush()


async def rotate_secret(
    db: AsyncSession, ctx: TenantContext, endpoint_id: uuid.UUID
) -> tuple[WebhookEndpoint, str]:
    endpoint = await get_endpoint(db, ctx, endpoint_id)
    secret = generate_secret()
    endpoint.secret_encrypted = get_secret_box().encrypt(secret)
    await audit.record(
        db,
        action="webhook.secret_rotated",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    return endpoint, secret


async def test_endpoint(
    db: AsyncSession, ctx: TenantContext, endpoint_id: uuid.UUID
) -> DeliveryResult:
    """Send a synchronous `ping` event so the customer can verify their receiver."""
    endpoint = await get_endpoint(db, ctx, endpoint_id)
    now = datetime.now(UTC)
    payload = build_envelope(
        event_id=uuid.uuid4(),
        event_type=PING_EVENT,
        organization_id=ctx.org_id,
        created_at=now,
        data={"endpoint_id": str(endpoint.id), "message": "Test delivery"},
    )
    async with http_client() as client:
        result = await send_event(
            client,
            url=endpoint.url,
            secret=_secret_of(endpoint),
            event_type=PING_EVENT,
            event_id=uuid.UUID(payload["id"]),
            payload=payload,
        )
    await audit.record(
        db,
        action="webhook.tested",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="webhook_endpoint",
        resource_id=endpoint.id,
        changes={"ok": result.ok, "status_code": result.status_code, "error": result.error},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    return result


# ---------------------------------------------------------------- deliveries


async def list_deliveries(
    db: AsyncSession,
    ctx: TenantContext,
    endpoint_id: uuid.UUID,
    *,
    status: DeliveryStatus | None,
    page: int,
    page_size: int,
) -> tuple[list[WebhookDelivery], int]:
    await get_endpoint(db, ctx, endpoint_id)
    base = select(WebhookDelivery).where(
        WebhookDelivery.organization_id == ctx.org_id, WebhookDelivery.endpoint_id == endpoint_id
    )
    if status is not None:
        base = base.where(WebhookDelivery.status == status)
    total = int(await db.scalar(select(func.count()).select_from(base.subquery())) or 0)
    rows = await db.execute(
        base.order_by(WebhookDelivery.created_at.desc(), WebhookDelivery.id)
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    return list(rows.scalars().all()), total


def redacted_payload(delivery: WebhookDelivery) -> dict[str, Any]:
    payload: dict[str, Any] = redact(delivery.payload)
    return payload


async def retry_delivery(
    db: AsyncSession, ctx: TenantContext, delivery_id: uuid.UUID
) -> WebhookDelivery:
    """Queue a failed or dead delivery again with a fresh attempt budget."""
    row = (
        await db.execute(
            select(WebhookDelivery, WebhookEndpoint)
            .join(WebhookEndpoint, WebhookEndpoint.id == WebhookDelivery.endpoint_id)
            .where(WebhookDelivery.organization_id == ctx.org_id, WebhookDelivery.id == delivery_id)
        )
    ).first()
    if row is None:
        raise NotFoundError("Delivery not found")
    delivery: WebhookDelivery = row[0]
    endpoint: WebhookEndpoint = row[1]
    if delivery.status not in (DeliveryStatus.FAILED, DeliveryStatus.DEAD):
        raise InvalidStateTransitionError("Only failed or dead deliveries can be retried")
    if not endpoint.is_active:
        raise InvalidStateTransitionError("Enable the endpoint before retrying its deliveries")
    delivery.status = DeliveryStatus.PENDING
    delivery.attempts = 0
    delivery.next_attempt_at = datetime.now(UTC)
    delivery.last_error = None
    await audit.record(
        db,
        action="webhook.delivery_retried",
        organization_id=ctx.org_id,
        actor_user_id=ctx.user_id,
        resource_type="webhook_delivery",
        resource_id=delivery.id,
        changes={"event_type": delivery.event_type, "event_id": str(delivery.event_id)},
        ip=ctx.meta.ip,
        user_agent=ctx.meta.user_agent,
    )
    await db.flush()
    await db.refresh(delivery)
    return delivery
