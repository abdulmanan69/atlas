"""Developer features: tenant API keys and outbound webhooks."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.v1.deps import DB, IntegrationsManage
from app.models.developer import DeliveryStatus, WebhookDelivery
from app.schemas.common import ERROR_RESPONSES, Page
from app.schemas.developer import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyOut,
    EventTypeOut,
    WebhookDeliveryOut,
    WebhookEndpointCreate,
    WebhookEndpointCreated,
    WebhookEndpointOut,
    WebhookEndpointUpdate,
    WebhookSecret,
    WebhookTestResult,
)
from app.services import api_keys as key_service
from app.services import outbound_webhooks as webhook_service

router = APIRouter(prefix="/developer", tags=["API & webhooks"], responses=ERROR_RESPONSES)


def _delivery_out(delivery: WebhookDelivery) -> WebhookDeliveryOut:
    out = WebhookDeliveryOut.model_validate(delivery)
    return out.model_copy(update={"payload": webhook_service.redacted_payload(delivery)})


@router.get(
    "/events", response_model=list[EventTypeOut], summary="Event types you can subscribe to"
)
async def list_event_types(ctx: IntegrationsManage) -> list[EventTypeOut]:
    return [EventTypeOut(type=t, description=d) for t, d in webhook_service.EVENT_CATALOG.items()]


# ------------------------------------------------------------------- API keys


@router.get(
    "/api-keys", response_model=list[ApiKeyOut], summary="List API keys (never the key itself)"
)
async def list_api_keys(
    ctx: IntegrationsManage, db: DB, include_revoked: bool = False
) -> list[ApiKeyOut]:
    keys = await key_service.list_api_keys(db, ctx, include_revoked=include_revoked)
    return [ApiKeyOut.model_validate(k) for k in keys]


@router.post(
    "/api-keys",
    response_model=ApiKeyCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create an API key (the full key is returned only in this response)",
)
async def create_api_key(data: ApiKeyCreate, ctx: IntegrationsManage, db: DB) -> ApiKeyCreated:
    key, raw = await key_service.create_api_key(db, ctx, data)
    return ApiKeyCreated(**ApiKeyOut.model_validate(key).model_dump(), key=raw)


@router.delete(
    "/api-keys/{key_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke an API key (immediate and permanent)",
)
async def revoke_api_key(key_id: uuid.UUID, ctx: IntegrationsManage, db: DB) -> None:
    await key_service.revoke_api_key(db, ctx, key_id)


# ------------------------------------------------------------------- webhooks


@router.get("/webhooks", response_model=list[WebhookEndpointOut])
async def list_webhooks(ctx: IntegrationsManage, db: DB) -> list[WebhookEndpointOut]:
    return [
        WebhookEndpointOut.model_validate(e) for e in await webhook_service.list_endpoints(db, ctx)
    ]


@router.post(
    "/webhooks",
    response_model=WebhookEndpointCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Register a webhook endpoint (the signing secret is returned only once)",
)
async def create_webhook(
    data: WebhookEndpointCreate, ctx: IntegrationsManage, db: DB
) -> WebhookEndpointCreated:
    endpoint, secret = await webhook_service.create_endpoint(db, ctx, data)
    return WebhookEndpointCreated(
        **WebhookEndpointOut.model_validate(endpoint).model_dump(), secret=secret
    )


@router.get("/webhooks/{endpoint_id}", response_model=WebhookEndpointOut)
async def get_webhook(
    endpoint_id: uuid.UUID, ctx: IntegrationsManage, db: DB
) -> WebhookEndpointOut:
    return WebhookEndpointOut.model_validate(
        await webhook_service.get_endpoint(db, ctx, endpoint_id)
    )


@router.patch(
    "/webhooks/{endpoint_id}",
    response_model=WebhookEndpointOut,
    summary="Change URL, subscriptions or enable/disable (enabling resets the failure counter)",
)
async def update_webhook(
    endpoint_id: uuid.UUID, data: WebhookEndpointUpdate, ctx: IntegrationsManage, db: DB
) -> WebhookEndpointOut:
    return WebhookEndpointOut.model_validate(
        await webhook_service.update_endpoint(db, ctx, endpoint_id, data)
    )


@router.delete("/webhooks/{endpoint_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(endpoint_id: uuid.UUID, ctx: IntegrationsManage, db: DB) -> None:
    await webhook_service.delete_endpoint(db, ctx, endpoint_id)


@router.post(
    "/webhooks/{endpoint_id}/test",
    response_model=WebhookTestResult,
    summary="Send a signed `ping` event now and report the endpoint's response",
)
async def test_webhook(
    endpoint_id: uuid.UUID, ctx: IntegrationsManage, db: DB
) -> WebhookTestResult:
    result = await webhook_service.test_endpoint(db, ctx, endpoint_id)
    return WebhookTestResult(
        ok=result.ok,
        status_code=result.status_code,
        latency_ms=result.latency_ms,
        error=result.error,
    )


@router.post(
    "/webhooks/{endpoint_id}/rotate-secret",
    response_model=WebhookSecret,
    summary="Replace the signing secret (returned only once)",
)
async def rotate_webhook_secret(
    endpoint_id: uuid.UUID, ctx: IntegrationsManage, db: DB
) -> WebhookSecret:
    _, secret = await webhook_service.rotate_secret(db, ctx, endpoint_id)
    return WebhookSecret(secret=secret)


@router.get(
    "/webhooks/{endpoint_id}/deliveries",
    response_model=Page[WebhookDeliveryOut],
    summary="Delivery history for an endpoint, newest first",
)
async def list_webhook_deliveries(
    endpoint_id: uuid.UUID,
    ctx: IntegrationsManage,
    db: DB,
    status_filter: Annotated[DeliveryStatus | None, Query(alias="status")] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Page[WebhookDeliveryOut]:
    items, total = await webhook_service.list_deliveries(
        db, ctx, endpoint_id, status=status_filter, page=page, page_size=page_size
    )
    return Page(
        items=[_delivery_out(d) for d in items], total=total, page=page, page_size=page_size
    )


@router.post(
    "/deliveries/{delivery_id}/retry",
    response_model=WebhookDeliveryOut,
    summary="Queue a failed or dead delivery again",
)
async def retry_delivery(
    delivery_id: uuid.UUID, ctx: IntegrationsManage, db: DB
) -> WebhookDeliveryOut:
    return _delivery_out(await webhook_service.retry_delivery(db, ctx, delivery_id))
