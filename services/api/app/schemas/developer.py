"""Contracts for tenant API keys and outbound webhooks."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field, field_validator

from app.models.developer import DeliveryStatus
from app.models.identity import Role
from app.schemas.common import APIModel
from app.services.events import EVENT_TYPES

WILDCARD_EVENT = "*"
SUBSCRIBABLE_EVENTS: tuple[str, ...] = (*EVENT_TYPES, WILDCARD_EVENT)

# ------------------------------------------------------------------- API keys


class ApiKeyCreate(APIModel):
    name: str = Field(min_length=1, max_length=120, examples=["Zapier sync"])
    role: Role = Field(description="Permissions the key acts with. Cannot exceed your own role.")
    expires_at: datetime | None = Field(
        default=None, description="Optional expiry. Keys without one are valid until revoked."
    )


class ApiKeyOut(APIModel):
    id: uuid.UUID
    name: str
    key_prefix: str = Field(description="First characters of the key, for recognition only")
    role: Role
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreated(ApiKeyOut):
    key: str = Field(description="The full key. It is shown only once; store it securely.")


# ------------------------------------------------------------------- webhooks


def _normalize_events(events: list[str]) -> list[str]:
    cleaned = sorted({e.strip() for e in events if e.strip()})
    if not cleaned:
        raise ValueError("at least one event type is required")
    unknown = [e for e in cleaned if e not in SUBSCRIBABLE_EVENTS]
    if unknown:
        raise ValueError(f"unknown event types: {', '.join(unknown)}")
    return cleaned


class WebhookEndpointCreate(APIModel):
    url: str = Field(
        min_length=8,
        max_length=2048,
        description="HTTPS URL on a public host",
        examples=["https://hooks.example.com/atlas"],
    )
    description: str | None = Field(default=None, max_length=500)
    events: list[str] = Field(
        min_length=1,
        description='Event types to receive, or ["*"] for all',
        examples=[["lead.created", "lead.stage_changed"]],
    )
    is_active: bool = True

    @field_validator("url")
    @classmethod
    def _strip_url(cls, value: str) -> str:
        return value.strip()

    @field_validator("events")
    @classmethod
    def _events(cls, value: list[str]) -> list[str]:
        return _normalize_events(value)


class WebhookEndpointUpdate(APIModel):
    url: str | None = Field(default=None, min_length=8, max_length=2048)
    description: str | None = Field(default=None, max_length=500)
    events: list[str] | None = Field(default=None, min_length=1)
    is_active: bool | None = Field(
        default=None, description="Re-enabling clears the failure counter"
    )

    @field_validator("url")
    @classmethod
    def _strip_url(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("events")
    @classmethod
    def _events(cls, value: list[str] | None) -> list[str] | None:
        return _normalize_events(value) if value is not None else None


class WebhookEndpointOut(APIModel):
    id: uuid.UUID
    url: str
    description: str | None
    events: list[str]
    is_active: bool
    failure_count: int = Field(description="Consecutive failed deliveries; reset on success")
    disabled_at: datetime | None
    disabled_reason: str | None
    created_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class WebhookEndpointCreated(WebhookEndpointOut):
    secret: str = Field(description="Signing secret. Shown only once; store it securely.")


class WebhookSecret(APIModel):
    secret: str = Field(description="New signing secret. The previous one stops working now.")


class WebhookTestResult(APIModel):
    ok: bool
    status_code: int | None
    latency_ms: int
    error: str | None


class WebhookDeliveryOut(APIModel):
    id: uuid.UUID
    endpoint_id: uuid.UUID
    event_type: str
    event_id: uuid.UUID
    payload: dict[str, Any]
    status: DeliveryStatus
    attempts: int
    next_attempt_at: datetime | None
    last_status_code: int | None
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None


class EventTypeOut(APIModel):
    type: str
    description: str
