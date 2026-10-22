"""Contracts for AI servers: self-hosted model hosts behind an AI gateway (ADR 0007)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from app.schemas.common import APIModel

AiServerStatus = Literal["ok", "degraded", "down", "unchecked"]
AiService = Literal["llm", "whisper", "tts", "live_stt"]
ServiceState = Literal["up", "loading", "down", "not_offered"]


def _secret(value: SecretStr | None, *, label: str, limit: int = 512) -> SecretStr | None:
    if value is None:
        return None
    raw = value.get_secret_value().strip()  # pasted values often carry a line break
    if not raw or len(raw) > limit or any(c.isspace() for c in raw):
        raise ValueError(f"Paste the {label}: up to {limit} characters, no spaces")
    return SecretStr(raw)


def _name(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        raise ValueError("Give the server a name")
    return cleaned


class AiServerCreate(APIModel):
    name: str = Field(min_length=1, max_length=60, description="Shown in ATLAS, e.g. 'GPU server'")
    url: str = Field(
        min_length=1,
        max_length=400,
        description=(
            "The AI gateway's address, e.g. https://ai.203-0-113-7.sslip.io. It must use "
            "https, unless the operator allows private model addresses "
            "(ATLAS_ALLOW_PRIVATE_MODEL_URLS) and the server is on a private network"
        ),
    )
    token: SecretStr = Field(
        description="The gateway's token (ATLAS_AI_TOKEN on the models host). Stored "
        "encrypted; never shown again"
    )
    live_stt_secret: SecretStr | None = Field(
        default=None,
        description=(
            "The secret in the live speech-to-text address: the part after /live-stt/ "
            "(ATLAS_AI_LIVE_STT_SECRET). Stored encrypted; never shown again"
        ),
    )
    deepgram_key: SecretStr | None = Field(
        default=None,
        description=(
            "A Deepgram API key, needed only for live speech-to-text: the voice service checks "
            "every Deepgram-type connection's key with Deepgram's cloud when it is saved, "
            "whatever its address. No audio goes to Deepgram. Kept only by the voice service"
        ),
    )

    _clean_name = field_validator("name")(_name)

    @field_validator("token")
    @classmethod
    def _token(cls, v: SecretStr) -> SecretStr | None:
        return _secret(v, label="token")

    @field_validator("live_stt_secret")
    @classmethod
    def _live(cls, v: SecretStr | None) -> SecretStr | None:
        return _secret(v, label="live speech-to-text secret", limit=200)

    @field_validator("deepgram_key")
    @classmethod
    def _deepgram(cls, v: SecretStr | None) -> SecretStr | None:
        return _secret(v, label="Deepgram key")

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        return v.strip()


class AiServerUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=60)
    token: SecretStr | None = Field(
        default=None,
        description="A new token. Checked with the server first; then every connection of "
        "this server uses it",
    )
    live_stt_secret: SecretStr | None = Field(
        default=None, description="A new live speech-to-text secret"
    )
    deepgram_key: SecretStr | None = Field(
        default=None,
        description="A Deepgram key for the live speech-to-text connection (see POST)",
    )

    _clean_name = field_validator("name")(_name)

    @field_validator("token")
    @classmethod
    def _token(cls, v: SecretStr | None) -> SecretStr | None:
        return _secret(v, label="token")

    @field_validator("live_stt_secret")
    @classmethod
    def _live(cls, v: SecretStr | None) -> SecretStr | None:
        return _secret(v, label="live speech-to-text secret", limit=200)

    @field_validator("deepgram_key")
    @classmethod
    def _deepgram(cls, v: SecretStr | None) -> SecretStr | None:
        return _secret(v, label="Deepgram key")


class AiServerUse(APIModel):
    llm_model: str | None = Field(
        default=None,
        max_length=200,
        description="Language model on the server; default: the first one it lists",
    )
    use_live_stt: bool = Field(
        default=True,
        description="Live speech-to-text when the server has it; otherwise Whisper",
    )
    fallback_llm_connection_id: str | None = Field(
        default=None,
        max_length=64,
        description="Another language model connection that answers when this server does not",
    )
    fallback_llm_model: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Model on the fallback connection; default: the model that connection is used "
            "with now, if it is the organization's current language model"
        ),
    )
    fallback_after_ms: int = Field(
        default=2500,
        ge=100,
        le=10000,
        description="Switch to the fallback when the server has given no output this long",
    )

    @field_validator("llm_model", "fallback_llm_connection_id", "fallback_llm_model")
    @classmethod
    def _blank(cls, v: str | None) -> str | None:
        return v.strip() or None if v is not None else None


class AiServerService(APIModel):
    """One model server behind the gateway, as the last check found it."""

    service: AiService
    title: str
    state: ServiceState = Field(
        description="'not_offered': the server does not run it; 'loading': starting its model"
    )
    up: bool
    latency_ms: int | None = None
    version: str | None = None
    models: list[str] = Field(default_factory=list)
    loaded: list[str] | None = Field(
        default=None, description="Language models in memory now; others load on first use"
    )
    error: str | None = Field(
        default=None, description="timeout, refused, unreachable, HTTP <code> or invalid response"
    )
    voices: int | None = None
    default_voice: str | None = None
    cloning: str | None = Field(default=None, description="Pocket TTS voice cloning state")
    sessions: int | None = None
    max_sessions: int | None = None
    connection_id: str | None = Field(
        default=None, description="The connection ATLAS created for it, if any"
    )


class AiServerOut(APIModel):
    id: uuid.UUID
    name: str
    url: str
    status: AiServerStatus = Field(
        description="ok: every service up; degraded: some up; down: none, or not reachable"
    )
    last_checked_at: datetime | None
    last_latency_ms: int | None = Field(description="Round trip of the last health check")
    last_error: str | None = Field(description="Why the last check failed")
    services: list[AiServerService]
    live_stt_secret_set: bool
    created_at: datetime
    updated_at: datetime


class AiServerSetup(APIModel):
    service: AiService
    created: bool
    connection_id: str | None = None
    connection_name: str | None = None
    reason: str | None = Field(default=None, description="Why no connection was created")


class AiServerRegistered(AiServerOut):
    setup: list[AiServerSetup] = Field(
        description="Per service: the connection created, or why none was"
    )
