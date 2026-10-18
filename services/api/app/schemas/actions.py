"""Contracts for Actions: HTTP calls an agent can make during a conversation.

An action maps 1:1 onto the voice service's HTTP tool definition. Credentials are
write-only: the service stores them and ATLAS only ever reports that one is configured.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from app.schemas.common import APIModel

HttpMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
ParameterType = Literal["string", "number", "boolean", "object", "array"]
BodyFormat = Literal["json", "form"]
AuthType = Literal["api_key", "bearer", "basic", "custom_header"]

MAX_TIMEOUT_MS = 15_000
DEFAULT_TIMEOUT_MS = 5_000
MAX_PARAMETERS = 30
MAX_HEADERS = 20
_IDENTIFIER = r"^[A-Za-z_][A-Za-z0-9_]*$"


class ActionParameter(APIModel):
    """A value the model supplies when it calls the action."""

    name: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER, examples=["date"])
    type: ParameterType = "string"
    description: str = Field(
        min_length=1,
        max_length=500,
        description="Tell the model what to provide and when",
        examples=["The viewing date the caller asked for, as YYYY-MM-DD"],
    )
    required: bool = True


class PresetParameter(APIModel):
    """A value ATLAS fills in from the call context, never asked of the model."""

    name: str = Field(min_length=1, max_length=64, pattern=_IDENTIFIER, examples=["phone"])
    type: ParameterType = "string"
    value: str = Field(
        min_length=1,
        max_length=1000,
        description=(
            "Fixed value or template. {{initial_context.x}} reads call-start context, "
            "{{gathered_context.x}} reads values captured during the call."
        ),
        examples=["{{initial_context.phone_number}}"],
    )
    required: bool = Field(default=True, description="Fail the call if the value is empty")


class ActionAuth(APIModel):
    """Credential sent with every request. Stored by the voice service; never returned."""

    type: AuthType
    header_name: str | None = Field(
        default=None, max_length=100, description="api_key and custom_header only"
    )
    api_key: str | None = Field(default=None, max_length=4000)
    token: str | None = Field(default=None, max_length=4000, description="bearer only")
    username: str | None = Field(default=None, max_length=1000, description="basic only")
    password: str | None = Field(default=None, max_length=4000, description="basic only")
    header_value: str | None = Field(default=None, max_length=4000, description="custom_header")

    @model_validator(mode="after")
    def _complete(self) -> ActionAuth:
        needed = {
            "api_key": ("header_name", "api_key"),
            "bearer": ("token",),
            "basic": ("username", "password"),
            "custom_header": ("header_name", "header_value"),
        }[self.type]
        missing = [f for f in needed if not (getattr(self, f) or "").strip()]
        if missing:
            raise ValueError(f"{self.type} authentication requires: {', '.join(missing)}")
        return self


class ActionAuthOut(APIModel):
    type: AuthType
    header_name: str | None = Field(description="Header the credential is sent in")
    status: Literal["configured"] = "configured"


class ActionSpec(APIModel):
    """Everything about an action except its credential."""

    name: str = Field(
        min_length=1,
        max_length=80,
        description="What the model sees; the function name is derived from it",
        examples=["Book a viewing"],
    )
    description: str = Field(
        min_length=1,
        max_length=2000,
        description="When the model should use it and what it gets back",
        examples=["Books a property viewing once the caller has confirmed a date and time."],
    )
    method: HttpMethod = "POST"
    url: str = Field(min_length=1, max_length=2048, examples=["https://api.example.com/viewings"])
    headers: dict[str, str] = Field(default_factory=dict, description="Static request headers")
    parameters: list[ActionParameter] = Field(default_factory=list, max_length=MAX_PARAMETERS)
    preset_parameters: list[PresetParameter] = Field(
        default_factory=list, max_length=MAX_PARAMETERS
    )
    timeout_ms: int = Field(default=DEFAULT_TIMEOUT_MS, ge=100, le=MAX_TIMEOUT_MS)
    body_format: BodyFormat = Field(
        default="json", description="Body encoding for POST, PUT and PATCH"
    )
    say_while_running: str | None = Field(
        default=None,
        max_length=300,
        description="Spoken by the agent while the request runs",
        examples=["Let me check that for you."],
    )

    @model_validator(mode="after")
    def _unique_parameter_names(self) -> ActionSpec:
        names = [p.name for p in self.parameters] + [p.name for p in self.preset_parameters]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"Parameter names must be unique: {', '.join(duplicates)}")
        if len(self.headers) > MAX_HEADERS:
            raise ValueError(f"At most {MAX_HEADERS} headers are allowed")
        return self


class ActionCreate(ActionSpec):
    auth: ActionAuth | None = None


class ActionUpdate(APIModel):
    """Partial update. Send `auth: null` to remove the credential; omit it to keep it."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = Field(default=None, min_length=1, max_length=2000)
    method: HttpMethod | None = None
    url: str | None = Field(default=None, min_length=1, max_length=2048)
    headers: dict[str, str] | None = None
    parameters: list[ActionParameter] | None = Field(default=None, max_length=MAX_PARAMETERS)
    preset_parameters: list[PresetParameter] | None = Field(default=None, max_length=MAX_PARAMETERS)
    timeout_ms: int | None = Field(default=None, ge=100, le=MAX_TIMEOUT_MS)
    body_format: BodyFormat | None = None
    say_while_running: str | None = Field(default=None, max_length=300)
    auth: ActionAuth | None = None


class ActionOut(ActionSpec):
    id: str = Field(description="Opaque id; attach to flow steps as a tool reference")
    function_name: str = Field(
        description="Name the model calls; must be unique per step and differ from path names"
    )
    auth: ActionAuthOut | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ActionTestRequest(APIModel):
    parameters: dict[str, Any] = Field(
        default_factory=dict, description="Sample values for the model-supplied parameters"
    )
    preset_parameters: dict[str, Any] = Field(
        default_factory=dict, description="Sample values for preset parameters"
    )


class ActionTestResult(APIModel):
    ok: bool
    status_code: int | None
    latency_ms: int
    request_method: str
    request_url: str
    response_excerpt: str | None = Field(description="First 1000 characters of the response")
    error: str | None = None
    hint: str | None = Field(default=None, description="Likely cause when the call failed")
