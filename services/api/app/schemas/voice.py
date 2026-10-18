"""Contracts for AI provider settings and voice agents."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.models.voice import AgentDirection, AgentStatus, SyncStatus
from app.schemas.common import APIModel

Scalar = str | int | float | bool | None
ServiceKind = Literal["llm", "stt", "tts", "embeddings"]

# ------------------------------------------------------------------ AI providers


class ProviderField(APIModel):
    name: str
    secret: bool


class ProviderOption(APIModel):
    id: str
    title: str
    self_hosted: bool
    credential_fields: list[str]
    connection_fields: list[str]
    credential_required: bool
    settings_schema: dict[str, Any] = Field(description="JSON Schema for this service's settings")


class ProviderCatalog(APIModel):
    llm: list[ProviderOption]
    stt: list[ProviderOption]
    tts: list[ProviderOption]
    embeddings: list[ProviderOption]


class ConnectionOut(APIModel):
    id: str
    name: str
    provider: str
    provider_title: str
    connection_settings: dict[str, Any]
    configured_credentials: list[str] = Field(description="Names of stored secrets, never values")
    hosted_trial: bool = False


class ConnectionCreate(APIModel):
    name: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    credentials: dict[str, str] = Field(default_factory=dict)
    connection_settings: dict[str, str] = Field(default_factory=dict)

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"name": "OpenAI", "provider": "openai", "credentials": {"api_key": "sk-..."}},
                {
                    "name": "Local LLM (Ollama)",
                    "provider": "speaches",
                    "connection_settings": {"base_url": "http://host.docker.internal:11434/v1"},
                },
            ]
        }
    }


class ConnectionUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    credentials: dict[str, str] | None = None
    connection_settings: dict[str, str] | None = None


class ServiceChoice(APIModel):
    connection_id: str = Field(min_length=1, max_length=64)
    settings: dict[str, Scalar] = Field(default_factory=dict)


class ModelSelection(APIModel):
    """Which connection and settings each service uses for this organization's calls."""

    llm: ServiceChoice | None = None
    stt: ServiceChoice | None = None
    tts: ServiceChoice | None = None
    embeddings: ServiceChoice | None = None


class ModelSelectionUpdate(APIModel):
    llm: ServiceChoice
    stt: ServiceChoice
    tts: ServiceChoice
    embeddings: ServiceChoice | None = None


class ProviderCheck(APIModel):
    ok: bool
    problems: list[str]


class VoiceOption(APIModel):
    id: str
    name: str
    gender: str | None = None
    accent: str | None = None
    language: str | None = None
    description: str | None = None
    preview_url: str | None = None


# ------------------------------------------------------------------ agents

_SLUG = r"^[a-z][a-z0-9_]{0,47}$"


class OutcomeDef(APIModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=300)


class CaptureField(APIModel):
    name: str = Field(pattern=_SLUG, description="Saved on the call record under this key")
    type: Literal["string", "number", "boolean"] = "string"
    description: str = Field(
        min_length=1, max_length=300, description="What the agent should capture"
    )


DEFAULT_OUTCOMES: list[dict[str, str]] = [
    {
        "code": "qualified",
        "description": "The person meets the qualification criteria and wants to proceed",
    },
    {
        "code": "callback_requested",
        "description": "The person asked to be called back at another time",
    },
    {"code": "not_interested", "description": "The person declined"},
    {"code": "wrong_number", "description": "The person is not who we were trying to reach"},
    {
        "code": "voicemail_detected",
        "description": "The call reached voicemail or an answering machine",
    },
    {"code": "do_not_call", "description": "The person asked never to be contacted again"},
]


class AgentModelOverride(APIModel):
    """Optional per-agent override of the organization's model selection."""

    llm: ServiceChoice | None = None
    tts: ServiceChoice | None = None


# ------------------------------------------------------------------ flows (ADR 0005)

NodeKind = Literal["start", "step", "end"]
MAX_FLOW_NODES = 60
MAX_FLOW_PATHS = 200


def path_function_name(name: str) -> str:
    """The function name the model calls to take a path: derived from the path's name."""
    return re.sub(r"[^a-z0-9]", "_", name.lower())


def _uuid_str(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise ValueError("Must be a UUID") from exc


def _named(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("A name is required")
    return cleaned


class FlowPosition(APIModel):
    x: float = 0
    y: float = 0


class FlowViewport(APIModel):
    x: float = 0
    y: float = 0
    zoom: float = Field(default=1, gt=0, le=4)


class FlowNode(APIModel):
    """One point in the conversation: the Start, a Step, or an End.

    Text that must be filled in before publishing (prompt, the Start greeting, a path's
    condition) may be left blank while drafting; `POST /agents/{id}/validate` and
    publishing report it with the node or path id and the field.
    """

    id: str = Field(description="Stable UUID chosen by the editor")
    kind: NodeKind
    name: str = Field(min_length=1, max_length=60)
    position: FlowPosition = Field(default_factory=FlowPosition)
    prompt: str = Field(default="", max_length=20_000, description="What the agent does here")
    greeting: str | None = Field(
        default=None, max_length=600, description="Start only: the first thing the agent says"
    )
    allow_interrupt: bool = True
    use_global_rules: bool = Field(
        default=True, description="Prepend the agent's global rules to this node's prompt"
    )
    capture_fields: list[CaptureField] = Field(default_factory=list, max_length=20)
    action_ids: list[str] = Field(
        default_factory=list, max_length=20, description="Actions the agent may use here"
    )
    knowledge_ids: list[str] = Field(
        default_factory=list, max_length=20, description="Documents the agent may answer from"
    )

    _id = field_validator("id")(_uuid_str)
    _name = field_validator("name")(_named)

    @model_validator(mode="after")
    def _consistent(self) -> FlowNode:
        names = [f.name for f in self.capture_fields]
        if len(names) != len(set(names)) or "opted_out" in names:
            raise ValueError("Capture field names must be unique and cannot be 'opted_out'")
        if len(set(self.action_ids)) != len(self.action_ids):
            raise ValueError("Actions are listed more than once")
        if len(set(self.knowledge_ids)) != len(self.knowledge_ids):
            raise ValueError("Documents are listed more than once")
        if self.kind != "start" and self.greeting is not None:
            raise ValueError("Only the Start has a greeting")
        return self


class FlowPath(APIModel):
    """A decision the agent can take from one node to another."""

    id: str
    from_node: str
    to_node: str
    name: str = Field(
        min_length=1, max_length=40, description="Short label; also names the model's function"
    )
    when: str = Field(default="", max_length=300, description="When the agent should take it")
    say_while_moving: str | None = Field(
        default=None, max_length=300, description="Spoken while moving to the next node"
    )

    _id = field_validator("id")(_uuid_str)
    _name = field_validator("name")(_named)


class FlowDefinition(APIModel):
    nodes: list[FlowNode] = Field(default_factory=list, max_length=MAX_FLOW_NODES)
    paths: list[FlowPath] = Field(default_factory=list, max_length=MAX_FLOW_PATHS)
    viewport: FlowViewport | None = None

    @model_validator(mode="after")
    def _unique_ids(self) -> FlowDefinition:
        node_ids = [n.id for n in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("Node ids must be unique")
        path_ids = [p.id for p in self.paths]
        if len(path_ids) != len(set(path_ids)):
            raise ValueError("Path ids must be unique")
        return self


IssueTarget = Literal["node", "path", "flow"]


class FlowIssue(APIModel):
    target: IssueTarget
    id: str = Field(description="Node or path id; empty for flow-level issues")
    field: str | None = None
    message: str


class FlowValidation(APIModel):
    valid: bool
    errors: list[FlowIssue]
    warnings: list[FlowIssue]


class CompiledPrompt(APIModel):
    """What the model is told at one node, exactly as it will be sent."""

    node_id: str
    node_name: str
    kind: NodeKind
    greeting: str | None = Field(default=None, description="Start only")
    prompt: str
    global_rules_applied: bool


# ------------------------------------------------------------------ call settings


class VoicemailSettings(APIModel):
    enabled: bool = Field(default=True, description="Detect answering machines on calls")
    action: Literal["hangup", "leave_message"] = "hangup"
    message: str | None = Field(default=None, max_length=1000)

    @model_validator(mode="after")
    def _message_when_leaving(self) -> VoicemailSettings:
        if self.enabled and self.action == "leave_message" and not (self.message or "").strip():
            raise ValueError("Write the message to leave on voicemail")
        return self


class AmbientSettings(APIModel):
    enabled: bool = False
    volume: float = Field(default=0.3, ge=0, le=1)


class CallSettings(APIModel):
    max_call_seconds: int = Field(default=300, ge=30, le=1200)
    idle_timeout_seconds: int = Field(
        default=10, ge=3, le=60, description="Hang up after this long without the person talking"
    )
    turn_start: Literal["default", "min_words"] = "default"
    turn_start_min_words: int = Field(default=2, ge=1, le=10)
    turn_stop: Literal["transcription", "turn_analyzer"] = "transcription"
    smart_turn_stop_seconds: float = Field(default=2.0, ge=0.5, le=5)
    key_terms: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="Names and terms speech-to-text should recognise",
    )
    voicemail: VoicemailSettings = Field(default_factory=VoicemailSettings)
    ambient: AmbientSettings = Field(default_factory=AmbientSettings)

    @field_validator("key_terms")
    @classmethod
    def _terms(cls, v: list[str]) -> list[str]:
        seen: list[str] = []
        for term in v:
            cleaned = " ".join(term.replace(",", " ").split())
            if not cleaned:
                continue
            if len(cleaned) > 60:
                raise ValueError("Each key term is limited to 60 characters")
            if cleaned not in seen:
                seen.append(cleaned)
        return seen


class AgentConfig(APIModel):
    """Everything about how an agent runs a call.

    `mode` chooses the authoring surface. In guided mode the greeting, instructions,
    questions, closing and end condition compile to a Start → End flow. In flow mode
    `flow` is authoritative; those five guided fields are kept only so switching was
    lossless and are ignored when the agent is published.
    """

    mode: Literal["guided", "flow"] = "guided"
    flow: FlowDefinition | None = Field(default=None, description="Required in flow mode")
    global_rules: str | None = Field(
        default=None, max_length=4000, description="Prepended to every node that opts in"
    )
    call_settings: CallSettings = Field(default_factory=CallSettings)
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
    company_name: str = Field(
        min_length=1, max_length=120, description="Who the agent calls on behalf of"
    )
    persona_name: str = Field(default="Alex", min_length=1, max_length=60)
    greeting: str = Field(min_length=1, max_length=600, description="First thing the agent says")
    instructions: str = Field(
        min_length=20, max_length=20_000, description="Goal, context and tone"
    )
    qualification_questions: list[str] = Field(default_factory=list, max_length=15)
    closing: str = Field(
        default="Thank them for their time, confirm any next step, and say goodbye.",
        min_length=1,
        max_length=1000,
    )
    end_condition: str = Field(
        default=(
            "The goal of the call is reached, the person wants to end the call, "
            "or the person is not interested."
        ),
        min_length=1,
        max_length=500,
    )
    outcomes: list[OutcomeDef] = Field(
        default_factory=lambda: [OutcomeDef(**o) for o in DEFAULT_OUTCOMES], max_length=20
    )
    capture_fields: list[CaptureField] = Field(default_factory=list, max_length=20)
    allow_interrupt: bool = True
    ai_disclosure: bool = Field(
        default=True, description="Agent confirms it is an AI assistant when asked"
    )
    model_override: AgentModelOverride | None = None

    @field_validator("qualification_questions")
    @classmethod
    def _questions(cls, v: list[str]) -> list[str]:
        cleaned = [q.strip() for q in v if q.strip()]
        if any(len(q) > 300 for q in cleaned):
            raise ValueError("Each question is limited to 300 characters")
        return cleaned

    @model_validator(mode="after")
    def _unique(self) -> AgentConfig:
        codes = [o.code for o in self.outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError("Outcome codes must be unique")
        names = [f.name for f in self.capture_fields]
        if len(names) != len(set(names)) or "opted_out" in names:
            raise ValueError("Capture field names must be unique and cannot be 'opted_out'")
        if self.mode == "flow" and self.flow is None:
            raise ValueError("A flow is required in flow mode")
        return self


class AgentCreate(APIModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    direction: AgentDirection = AgentDirection.OUTBOUND
    template: str | None = Field(default=None, description="Start from a template id")
    config: AgentConfig | None = None


class AgentMetaUpdate(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class AgentConfigSave(APIModel):
    config: AgentConfig
    note: str | None = Field(default=None, max_length=300)


class AgentOut(APIModel):
    id: uuid.UUID
    name: str
    description: str | None
    direction: AgentDirection
    status: AgentStatus
    latest_version: int
    published_version: int | None
    sync_status: SyncStatus
    sync_error: str | None
    synced_at: datetime | None
    created_at: datetime
    updated_at: datetime


class AgentDetail(AgentOut):
    config: AgentConfig
    has_unpublished_changes: bool


class AgentVersionOut(APIModel):
    version: int
    note: str | None
    created_at: datetime
    created_by_user_id: uuid.UUID | None
    is_published: bool


class PublishRequest(APIModel):
    version: int | None = Field(default=None, description="Defaults to the latest version")
    note: str | None = Field(default=None, max_length=300)


class AgentTemplate(APIModel):
    id: str
    name: str
    description: str
    direction: AgentDirection
    config: AgentConfig


class TestSessionRequest(APIModel):
    lead_id: uuid.UUID | None = Field(
        default=None, description="Fill template variables from this lead"
    )


class IceServer(APIModel):
    urls: str | list[str]
    username: str | None = None
    credential: str | None = None


class TestSession(APIModel):
    session_token: str
    signaling_url: str
    ice_servers: list[IceServer]
    expires_in_seconds: int
    variables: dict[str, str]
