"""Call contracts: start a call, history, detail, events, transcript, webhook receipt."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from app.models.calls import Call, CallDirection, CallMode, CallState
from app.schemas.common import APIModel


class CallStart(APIModel):
    lead_id: uuid.UUID
    agent_id: uuid.UUID
    live: bool = Field(
        default=False,
        description=(
            "Place a real phone call. Requires the campaigns:launch_live permission and "
            "live calling enabled on the deployment. Default: a simulated dry run that "
            "produces a synthetic, clearly labelled result."
        ),
    )


def _transcript_available(call: Call) -> bool:
    if call.transcript:
        return True
    return call.mode != CallMode.SIMULATED and bool(call.runtime_transcript_key)


class CallOut(APIModel):
    id: uuid.UUID
    lead_id: uuid.UUID | None
    lead_name: str | None = Field(
        default=None,
        description="The lead's display name at read time; null for unknown or deleted leads",
    )
    agent_id: uuid.UUID | None
    agent_version: int | None
    campaign_id: uuid.UUID | None
    direction: CallDirection
    mode: CallMode
    state: CallState
    outcome: str | None = Field(description="Outcome code from the agent's configured outcomes")
    to_phone_e164: str | None
    from_phone_e164: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    opted_out: bool
    recording_available: bool
    transcript_available: bool = Field(description="GET /calls/{id}/transcript returns text")
    captured_count: int = Field(
        default=0, description="Number of values the agent captured; the values are on the detail"
    )
    error_code: str | None
    error_message: str | None
    initiated_by_user_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def _from_record(cls, value: Any) -> Any:
        if isinstance(value, Call):
            data = {name: getattr(value, name) for name in cls.model_fields if hasattr(value, name)}
            data["transcript_available"] = _transcript_available(value)
            data["captured_count"] = len(value.captured or {})
            return data
        return value


class CallDetail(CallOut):
    captured: dict[str, Any] = Field(description="Values the agent captured during the call")
    summary: str | None
    usage: dict[str, Any] = Field(description="Model and call usage reported for the call")
    estimated_cost: float | None = Field(description="Estimated cost, when known")


class CallTranscript(APIModel):
    call_id: uuid.UUID
    mode: CallMode
    simulated: bool = Field(description="True for a dry run: the text is synthetic")
    text: str


class CallEventOut(APIModel):
    id: uuid.UUID
    type: str
    data: dict[str, Any]
    occurred_at: datetime


class WebhookReceipt(APIModel):
    status: Literal["accepted", "duplicate", "processed"]
