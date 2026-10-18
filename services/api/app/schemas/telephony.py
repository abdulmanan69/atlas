"""Phone numbers: calling providers, numbers, inbound routing and setup instructions."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import Field

from app.schemas.common import APIModel

Connectivity = Literal["api", "sip"]


class CredentialField(APIModel):
    name: str
    label: str
    secret: bool = Field(description="Stored by the voice service; never shown again")
    required: bool = True
    multiline: bool = False
    help: str | None = None


class TelephonyProviderOption(APIModel):
    id: str
    title: str
    connectivity: Connectivity
    supports_outbound: bool
    fields: list[CredentialField]


class ProviderOut(APIModel):
    id: str
    name: str
    provider: str
    provider_title: str
    connectivity: Connectivity = "api"
    configured_fields: list[str] = Field(
        description="Names of the credential fields on file; values are never returned"
    )
    is_default_outbound: bool = False
    inactive: bool = False
    inactive_reason: str | None = None
    number_count: int = 0
    supports_outbound: bool = True
    ready_for_outbound: bool = True
    outbound_blocked_reason: str | None = None
    supports_number_sync: bool = Field(
        default=False, description="The provider can list the numbers its account holds"
    )


class ProviderCreate(APIModel):
    name: str = Field(min_length=1, max_length=64, examples=["Main Twilio account"])
    provider: str = Field(min_length=1, max_length=32, examples=["twilio"])
    credentials: dict[str, str] = Field(
        default_factory=dict, description="Exactly the fields listed in the catalog"
    )
    is_default_outbound: bool = Field(
        default=False, description="Use this account for outbound calls unless told otherwise"
    )


class ProviderUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=64)
    credentials: dict[str, str] | None = Field(
        default=None, description="Only the fields to replace; others stay as they are"
    )


class ProviderSyncOut(APIModel):
    ok: bool
    message: str | None = None


class NumberOut(APIModel):
    id: str
    provider_id: str
    provider: str
    provider_name: str
    phone_number: str = Field(description="E.164")
    display: str
    label: str | None = None
    inbound_agent_id: uuid.UUID | None = Field(
        default=None, description="Agent that answers calls to this number"
    )
    inbound_agent_name: str | None = None
    is_active: bool = True
    is_default_caller_id: bool = False
    provider_sync: ProviderSyncOut | None = Field(
        default=None,
        description="Result of pushing the inbound webhook to the provider, when attempted",
    )


class NumberCreate(APIModel):
    phone_number: str = Field(min_length=3, max_length=32, examples=["+14155550100"])
    label: str | None = Field(default=None, max_length=64)
    default_region: str = Field(
        default="US", min_length=2, max_length=2, description="ISO-2 region for local formats"
    )


class InboundAgentAssignment(APIModel):
    agent_id: uuid.UUID | None = Field(
        description="Published agent that answers inbound calls on this number; null to stop"
    )


class SkippedNumberOut(APIModel):
    phone_number: str
    reason: str


class NumberSyncResult(APIModel):
    added: list[NumberOut]
    updated: list[NumberOut]
    skipped: list[SkippedNumberOut]
    missing_at_provider: list[NumberOut] = Field(
        description="Numbers on file the provider no longer lists; nothing is removed"
    )


class SetupInstructions(APIModel):
    provider: str
    title: str
    inbound_webhook_url: str = Field(description="Where the provider sends incoming calls")
    fallback_webhook_url: str | None = None
    event_webhook_url: str | None = None
    automatic: bool = Field(
        description="The voice service writes the webhook into the provider itself when an "
        "agent is assigned; the manual steps are a fallback"
    )
    steps: list[str]
    notes: list[str]
