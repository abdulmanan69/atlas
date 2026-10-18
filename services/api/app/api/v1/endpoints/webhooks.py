"""Inbound events from the voice service (server-to-server; no user session)."""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Header, Request, status

from app.api.v1.deps import DB
from app.core.exceptions import ValidationFailedError
from app.core.redis import enforce_rate_limit
from app.integrations.voice_runtime.webhooks import DELIVERY_ID_HEADER, RUN_ID_HEADER
from app.integrations.voice_runtime.workflow_builder import WEBHOOK_SECRET_HEADER
from app.schemas.calls import WebhookReceipt
from app.schemas.common import ErrorResponse
from app.services import webhooks as service

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
_PER_IP_PER_MINUTE = 1200


@router.post(
    "/voice-runtime",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=WebhookReceipt,
    summary="Call event from the voice service",
    description=(
        "Called by the voice service when a call finishes. Authenticated with the shared "
        "secret issued when the agent was published; the body is only used to route the "
        "event, the result itself is fetched back from the voice service. Deliveries are "
        "idempotent: a repeated delivery id is acknowledged and ignored."
    ),
    responses={
        401: {"model": ErrorResponse, "description": "Secret missing or wrong"},
        404: {"model": ErrorResponse, "description": "Event does not match a known call"},
        422: {"model": ErrorResponse, "description": "Malformed event"},
    },
)
async def voice_runtime_event(
    request: Request,
    db: DB,
    secret: Annotated[
        str | None,
        Header(alias=WEBHOOK_SECRET_HEADER, description="Shared secret for this organization"),
    ] = None,
) -> WebhookReceipt:
    client_ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(f"webhook:{client_ip}", _PER_IP_PER_MINUTE)
    body = await request.body()
    try:
        payload = json.loads(body) if body else {}
    except ValueError as exc:
        raise ValidationFailedError("Body must be JSON") from exc
    receipt = await service.receive_runtime_event(
        db,
        service.Delivery(
            secret=secret,
            delivery_id=request.headers.get(DELIVERY_ID_HEADER),
            run_id_header=request.headers.get(RUN_ID_HEADER),
            body=body,
            payload=payload,
        ),
    )
    return WebhookReceipt(status=receipt.status)
