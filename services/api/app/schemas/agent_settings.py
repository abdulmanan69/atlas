"""Contracts for agent sound files, setting secrets and the website widget."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, SecretStr, field_validator

from app.schemas.common import APIModel
from app.schemas.voice import IceServer

AudioPurpose = Literal["ambient", "message"]
MAX_ALLOWED_DOMAINS = 20

_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def normalize_domain(raw: str) -> str:
    """`https://www.Example.com/path` → `www.example.com`.

    Accepts a host, optionally `*.` for any subdomain and `:port`; refuses a bare `*`.
    """
    shown = raw.strip()
    value = re.sub(r"^[a-z][a-z0-9+.-]*://", "", shown.lower())
    value = value.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0].rstrip(".")
    problem = f"'{shown}' is not a website domain, e.g. example.com or *.example.com"
    wildcard = value.startswith("*.")
    host = value[2:] if wildcard else value
    if ":" in host:
        host, port = host.rsplit(":", 1)
        if not port.isdigit() or not 0 < int(port) < 65536:
            raise ValueError(f"'{shown}' has an invalid port")
    labels = host.split(".")
    if not host or len(host) > 253 or not all(_LABEL.match(label) for label in labels):
        raise ValueError(problem)
    if wildcard and len(labels) < 2:
        raise ValueError(problem)
    return value


class AgentAudioOut(APIModel):
    id: uuid.UUID
    purpose: AudioPurpose
    filename: str
    content_type: str
    size_bytes: int
    duration_seconds: float | None = Field(description="WAV files only; null when unknown")
    created_at: datetime


class ClassifierKeyIn(APIModel):
    connection_id: str = Field(min_length=1, max_length=64)
    api_key: SecretStr = Field(description="Stored encrypted; never returned")

    @field_validator("api_key")
    @classmethod
    def _present(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value().strip()
        if not raw or len(raw) > 500:
            raise ValueError("Enter the API key (up to 500 characters)")
        return SecretStr(raw)


class ClassifierKeyOut(APIModel):
    connection_id: str
    updated_at: datetime


class WebEmbedUpdate(APIModel):
    enabled: bool
    allowed_domains: list[str] = Field(
        default_factory=list,
        max_length=MAX_ALLOWED_DOMAINS,
        description="Sites allowed to show the widget: example.com, *.example.com, localhost:3000",
    )

    @field_validator("allowed_domains")
    @classmethod
    def _domains(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for raw in v:
            if not raw.strip():
                continue
            domain = normalize_domain(raw)
            if domain not in out:
                out.append(domain)
        return out


class WebEmbedOut(APIModel):
    enabled: bool
    allowed_domains: list[str]
    key: str | None = Field(description="Public widget key; null until the widget is first set up")
    script_url: str
    snippet: str | None = Field(description="Paste before </body> on the allowed sites")
    sessions_started: int
    last_used_at: datetime | None
    rotated_at: datetime | None
    agent_published: bool = Field(description="Visitors can only talk to a published, active agent")


class WebCallSession(APIModel):
    """What the website widget needs to connect a visitor's browser to the agent."""

    call_id: uuid.UUID
    call_token: str = Field(description="Proves the caller started this call; used to end it")
    signaling_url: str
    ice_servers: list[IceServer]
    expires_in_seconds: int
    agent_name: str
    company_name: str


class WebCallEnd(APIModel):
    call_token: str = Field(min_length=10, max_length=200)
    connected: bool
