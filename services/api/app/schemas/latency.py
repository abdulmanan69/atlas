"""Response-time contracts (ADR 0006): per-call latency, fast-stack guide, pipeline health."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from app.schemas.common import APIModel

LatencySource = Literal["runtime", "simulated"]
ToolKind = Literal["knowledge", "action"]

TARGET_MS = 800
SLOW_MS = 1500


class LatencySummary(APIModel):
    """One call's response times. Values are milliseconds; null when not measured."""

    source: LatencySource = Field(
        description="runtime = measured on a real call; simulated = synthetic dry-run values"
    )
    turns: int = Field(description="Conversation turns seen in the call")
    measured_turns: int = Field(description="Turns with a measured response time")
    response_p50_ms: int | None = Field(
        description="Median time from the person finishing speaking to the agent starting to speak"
    )
    response_p95_ms: int | None = Field(description="95th percentile response time")
    response_max_ms: int | None = Field(description="Slowest response in the call")
    first_response_ms: int | None = Field(
        description="Response time of the first answered turn in the call"
    )
    stt_ttfb_p50_ms: int | None = Field(description="Median speech-to-text time to first result")
    llm_ttfb_p50_ms: int | None = Field(description="Median language-model time to first token")
    tts_ttfb_p50_ms: int | None = Field(description="Median text-to-speech time to first audio")
    tool_time_ms: int = Field(description="Total time spent in actions and knowledge lookups")
    slow_turns: int = Field(description=f"Turns whose response took longer than {SLOW_MS} ms")
    interruptions: int = Field(description="Times the person started speaking over the agent")


class LatencyTool(APIModel):
    name: str
    kind: ToolKind = Field(description="knowledge = document lookup; action = an ATLAS action")
    duration_ms: int | None


class LatencyTurn(APIModel):
    turn: int
    node_name: str | None
    user_text: str | None
    agent_text: str | None
    user_started_at: datetime | None
    user_ended_at: datetime | None
    agent_started_at: datetime | None
    agent_ended_at: datetime | None
    response_ms: int | None = Field(
        description="Person stops speaking → agent starts speaking, in milliseconds"
    )
    stt_ttfb_ms: int | None
    llm_ttfb_ms: int | None
    tts_ttfb_ms: int | None
    tools: list[LatencyTool] = Field(default_factory=list)
    slow: bool = Field(description=f"response_ms above {SLOW_MS}")


class LatencyThresholds(APIModel):
    target_ms: int = TARGET_MS
    slow_ms: int = SLOW_MS


class CallLatencyDetail(APIModel):
    summary: LatencySummary
    turns: list[LatencyTurn]
    thresholds: LatencyThresholds = Field(default_factory=LatencyThresholds)


# ------------------------------------------------------------------ fast-stack guide

GuideKind = Literal["stt", "llm", "tts"]


class LatencyGuideProvider(APIModel):
    kind: GuideKind
    provider: str = Field(description="Provider id, as in the provider catalog")
    model_hint: str | None = Field(description="Model or option the figure refers to")
    typical_ms: int | None = Field(
        description=(
            "Typical latency as stated by the provider (not measured by ATLAS); "
            "null when the provider states no figure or it depends on your hardware"
        )
    )
    streaming: bool = Field(description="Results stream as they are produced")
    turn_aware: bool = Field(
        description="Speech-to-text detects the end of the person's turn itself"
    )
    note: str


class PresetChoice(APIModel):
    provider: str
    settings: dict[str, Any] = Field(default_factory=dict)


class PresetChoices(APIModel):
    stt: PresetChoice
    llm: PresetChoice
    tts: PresetChoice


class LatencyPreset(APIModel):
    id: str
    title: str
    description: str
    choices: PresetChoices
    requires: list[str] = Field(description="Provider ids that need a connection first")


class LatencyGuide(APIModel):
    providers: list[LatencyGuideProvider]
    presets: list[LatencyPreset]
    tts_cache_providers: list[str] = Field(
        description="Text-to-speech providers for which the speech cache can be turned on"
    )
    disclaimer: str


# ------------------------------------------------------------------ pipeline health


class ServiceLatency(APIModel):
    service: str = Field(description="Service class reported by the voice service")
    kind: str | None = Field(default=None, description="stt, llm or tts")
    p50_ms: int | None
    p95_ms: int | None
    samples: int


class ResponseLatency(APIModel):
    p50_ms: int | None
    p95_ms: int | None
    samples: int


class PipelineHealth(APIModel):
    available: bool = Field(description="False when metrics are not enabled or not reachable")
    scope: Literal["platform"] = "platform"
    window: Literal["process lifetime"] = "process lifetime"
    services: list[ServiceLatency] = Field(default_factory=list)
    response: ResponseLatency | None = None
    note: str
