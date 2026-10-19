"""Response times (ADR 0006): event-log parser, sync, simulated latency, endpoints, presets.

Event fixtures follow the runtime v1.49 shapes exactly:
`{type, timestamp, turn?, node_id?, node_name?, payload}` (realtime_feedback_events.py:154-180,
in_memory_buffers.py:150-185); user transcription / bot text payloads per
realtime_feedback_events.py:28-67; ttfb per :110-131; function calls per :69-108;
`rtf-latency-measured` payload `{latency_seconds}` per run_pipeline.py:1259-1280.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.core.config import get_settings
from app.db.session import get_sessionmaker
from app.integrations.voice_runtime.simulated import SimulatedResult
from app.integrations.voice_runtime.workflow_builder import build_configurations
from app.models.calls import Call, CallDirection, CallMode, CallState
from app.models.voice import Agent, AgentDirection, AgentStatus
from app.schemas.latency import CallLatencyDetail
from app.schemas.voice import AgentConfig, CallSettings, FlowDefinition
from app.services import ai_speed, latency
from app.services import calls as call_service
from app.services.flows import validate_flow
from app.workers.calls import complete_simulated_call
from tests.calls_support import (
    CallsRuntime,
    create_lead,
    enable_live_calls,
    make_runtime,
    published_agent,
    start_call,
)
from tests.conftest import Account, invite_and_join, signup
from tests.fake_runtime import VALID_KEY

T0 = datetime(2026, 10, 19, 10, 0, 0, tzinfo=UTC)


def at(ms: int) -> str:
    """ISO-8601 UTC with milliseconds, as the runtime stamps events."""
    return (T0 + timedelta(milliseconds=ms)).isoformat(timespec="milliseconds")


def ev(type_: str, ts: int, payload: dict[str, Any], turn: int | None = None) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": type_,
        "timestamp": at(ts),
        "node_id": "n1",
        "node_name": "Conversation",
        "payload": payload,
    }
    if turn is not None:
        event["turn"] = turn
    return event


def user(turn: int, start: int, end: int, text: str) -> dict[str, Any]:
    return ev(
        "rtf-user-transcription",
        end,
        {"text": text, "final": True, "timestamp": at(start), "end_timestamp": at(end)},
        turn,
    )


def bot(turn: int, start: int, end: int, text: str) -> dict[str, Any]:
    return ev(
        "rtf-bot-text",
        start,
        {"text": text, "timestamp": at(start), "end_timestamp": at(end)},
        turn,
    )


def ttfb(turn: int, ts: int, seconds: float, processor: str, kind: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {"ttfb_seconds": seconds, "processor": processor, "model": "m"}
    if kind is not None:
        payload["kind"] = kind
    return ev("rtf-ttfb-metric", ts, payload, turn)


def conversation() -> list[dict[str, Any]]:
    """Greeting, a measured turn, a knowledge lookup turn, an interruption, a slow turn."""
    return [
        # turn 0: the greeting, nobody answered
        bot(0, 500, 3_000, "Hi Hina, this is Alex from Acme."),
        # turn 1: measured by the runtime (latency 0.62 s) plus per-stage TTFB
        ev("rtf-user-transcription", 4_200, {"text": "Hi", "final": False}, 1),
        user(1, 3_500, 4_400, "Hi, yes."),
        ttfb(1, 4_500, 0.18, "DeepgramSTTService#3", None),  # no kind: suffix says STT
        ttfb(1, 4_700, 0.41, "OpenAILLMService#1", "llm"),
        ttfb(1, 4_900, 0.12, "ElevenLabsTTSService#2", None),
        ev("rtf-latency-measured", 5_020, {"latency_seconds": 0.62}, 1),
        bot(1, 5_020, 8_000, "Great. Are you still looking for a house?"),
        # turn 2: no measured value, so bot start - user end; a knowledge lookup
        user(2, 8_500, 10_000, "What areas do you cover?"),
        ev(
            "rtf-function-call-start",
            10_100,
            {
                "function_name": "retrieve_from_knowledge_base",
                "tool_call_id": "call_1",
                "arguments": {"query": "areas"},
            },
            2,
        ),
        ev(
            "rtf-function-call-end",
            10_450,
            {
                "function_name": "retrieve_from_knowledge_base",
                "tool_call_id": "call_1",
                "result": "x",
            },
            2,
        ),
        ev("rtf-ttfb-metric", 10_600, {"ttfb_seconds": 0.5, "processor": None, "model": None}, 2),
        bot(2, 10_900, 15_000, "We cover the whole city."),
        # turn 3: the person starts over the agent (inside 10.9-15.0 s); slow reply
        user(3, 14_000, 15_500, "Do you do rentals?"),
        ev(
            "rtf-function-call-start",
            15_600,
            {"function_name": "book_viewing", "tool_call_id": "call_2"},
            3,
        ),
        ev(
            "rtf-function-call-end",
            16_600,
            {"function_name": "book_viewing", "tool_call_id": "call_2"},
            3,
        ),
        ev("rtf-latency-measured", 17_600, {"latency_seconds": 2.1}, 3),
        bot(3, 17_600, 20_000, "We do, and I booked a viewing."),
    ]


# ------------------------------------------------------------------ parser


def test_parser_builds_turns_and_summary() -> None:
    result = latency.parse_events(conversation())
    assert result is not None
    detail = CallLatencyDetail.model_validate(result)  # stored shape matches the contract
    turns = {t.turn: t for t in detail.turns}
    assert sorted(turns) == [0, 1, 2, 3]

    greeting = turns[0]
    assert greeting.user_text is None and greeting.response_ms is None and not greeting.slow
    assert greeting.agent_started_at == T0 + timedelta(milliseconds=500)

    first = turns[1]
    assert first.user_text == "Hi, yes.", "interim transcripts are ignored"
    assert first.response_ms == 620, "runtime-measured value wins"
    assert (first.stt_ttfb_ms, first.llm_ttfb_ms, first.tts_ttfb_ms) == (180, 410, 120)
    assert first.user_ended_at == T0 + timedelta(milliseconds=4_400)
    assert first.node_name == "Conversation"

    lookup = turns[2]
    assert lookup.response_ms == 900, "10.9 s bot start - 10.0 s user end"
    assert lookup.llm_ttfb_ms == 500, "missing kind and processor -> llm"
    assert [(t.name, t.kind, t.duration_ms) for t in lookup.tools] == [
        ("retrieve_from_knowledge_base", "knowledge", 350)
    ]

    slow = turns[3]
    assert slow.response_ms == 2_100 and slow.slow is True
    assert [(t.name, t.kind, t.duration_ms) for t in slow.tools] == [
        ("book_viewing", "action", 1_000)
    ]

    s = detail.summary
    assert s.source == "runtime" and s.turns == 4 and s.measured_turns == 3
    assert (s.response_p50_ms, s.response_max_ms, s.first_response_ms) == (900, 2_100, 620)
    assert s.response_p95_ms == 1980  # linear interpolation between 900 and 2100
    assert (s.stt_ttfb_p50_ms, s.llm_ttfb_p50_ms, s.tts_ttfb_p50_ms) == (180, 455, 120)
    assert s.tool_time_ms == 1_350 and s.slow_turns == 1 and s.interruptions == 1
    assert detail.thresholds.target_ms == 800 and detail.thresholds.slow_ms == 1500


def test_ttfb_kind_classifies_by_suffix_not_substring() -> None:
    assert latency.ttfb_kind({"processor": "DeepgramSTTService#3"}) == "stt"
    assert latency.ttfb_kind({"processor": "CartesiaSTTService"}) == "stt"
    assert latency.ttfb_kind({"processor": "ElevenLabsTTSService#2"}) == "tts"
    assert latency.ttfb_kind({"processor": "OpenAILLMService#1"}) == "llm"
    assert latency.ttfb_kind({}) == "llm", "events before STT/TTS forwarding are LLM"
    assert latency.ttfb_kind({"kind": "tts", "processor": "DeepgramSTTService"}) == "tts"
    assert latency.ttfb_kind({"kind": "weird", "processor": "XTTSService"}) == "tts"


@pytest.mark.parametrize(
    "logs",
    [
        None,
        "nope",
        {},
        {"realtime_feedback_events": None},
        {"realtime_feedback_events": "x"},
        {"realtime_feedback_events": []},
        {"realtime_feedback_events": [1, "x", None, {"type": 5}]},
        # only metrics, no conversation
        {"realtime_feedback_events": [ttfb(1, 0, 0.2, "X", "llm")]},
    ],
)
def test_parser_returns_none_without_a_conversation(logs: Any) -> None:
    assert latency.from_run_logs(logs) is None


def test_parser_survives_malformed_events() -> None:
    events: list[Any] = [
        {"type": "rtf-bot-text", "timestamp": "garbage", "payload": "not-a-dict", "turn": "x"},
        {
            "type": "rtf-user-transcription",
            "payload": {"text": "hi", "final": True, "timestamp": "bad", "end_timestamp": 5},
        },
        {"type": "rtf-latency-measured", "turn": 1, "payload": {"latency_seconds": "fast"}},
        {"type": "rtf-latency-measured", "turn": 1, "payload": {"latency_seconds": -1}},
        {"type": "rtf-latency-measured", "turn": 1, "payload": {"latency_seconds": float("nan")}},
        {"type": "rtf-ttfb-metric", "turn": 1, "payload": {"ttfb_seconds": True}},
        {"type": "rtf-function-call-end", "payload": {"tool_call_id": "never-started"}},
        {
            "type": "rtf-function-call-start",
            "turn": 1,
            "payload": {"function_name": "lookup", "tool_call_id": "open"},
        },
        bot(1, 2_000, 1_000, "ends before it starts"),
        {"type": "rtf-bot-text", "timestamp": at(0), "turn": True, "payload": {"text": 7}},
    ]
    result = latency.parse_events(events)
    assert result is not None
    detail = CallLatencyDetail.model_validate(result)
    assert detail.summary.measured_turns == 0 and detail.summary.response_p50_ms is None
    open_tool = [t for turn in detail.turns for t in turn.tools]
    assert [(t.name, t.duration_ms) for t in open_tool] == [("lookup", None)]


def test_turn_count_is_bounded() -> None:
    events = [bot(i, i * 10, i * 10 + 5, "x") for i in range(latency.MAX_TURNS + 50)]
    result = latency.parse_events(events)
    assert result is not None and len(result["turns"]) == latency.MAX_TURNS


def test_percentile() -> None:
    assert latency.percentile([], 0.5) is None
    assert latency.percentile([7], 0.95) == 7
    assert latency.percentile([100, 200, 300, 400], 0.5) == 250
    assert latency.percentile([100, 200, 300, 400], 0.95) == 385


def test_simulated_latency_is_deterministic_and_labelled() -> None:
    a = latency.simulated("seed-1", duration_seconds=90, started_at=T0)
    b = latency.simulated("seed-1", duration_seconds=90, started_at=T0)
    c = latency.simulated("seed-2", duration_seconds=90, started_at=T0)
    assert a == b and a != c
    detail = CallLatencyDetail.model_validate(a)
    assert detail.summary.source == "simulated" and detail.summary.turns == 6
    assert detail.summary.measured_turns == 5 and detail.turns[0].response_ms is None


# ------------------------------------------------------------------ call settings & flows


def test_response_speed_presets_compile() -> None:
    def compiled(**cs: Any) -> dict[str, Any]:
        cfg = AgentConfig(
            company_name="Acme",
            greeting="Hi",
            instructions="Qualify the lead politely and briefly.",
            call_settings=CallSettings.model_validate(cs),
        )
        return build_configurations(cfg)

    default = compiled()
    assert CallSettings().response_speed == "balanced"
    assert (
        default["turn_start_strategy"],
        default["turn_start_min_words"],
        default["turn_stop_strategy"],
        default["smart_turn_stop_secs"],
        default["max_user_idle_timeout"],
    ) == ("min_words", 2, "transcription", 1.5, 10.0)
    assert default["context_compaction_enabled"] is True
    assert default["tts_cache_enabled"] is False
    assert default["transcript_configuration"] == {"include_end_timestamps": True}

    snappy = compiled(response_speed="snappy", turn_stop="turn_analyzer")
    assert (snappy["turn_start_min_words"], snappy["turn_stop_strategy"]) == (1, "transcription")
    assert (snappy["smart_turn_stop_secs"], snappy["max_user_idle_timeout"]) == (0.8, 8.0)
    patient = compiled(response_speed="patient", context_compaction=False, tts_cache=True)
    assert (patient["turn_start_strategy"], patient["turn_stop_strategy"]) == (
        "default",
        "turn_analyzer",
    )
    assert (patient["smart_turn_stop_secs"], patient["max_user_idle_timeout"]) == (2.5, 15.0)
    assert patient["context_compaction_enabled"] is False and patient["tts_cache_enabled"] is True

    custom = compiled(response_speed="custom", turn_stop="turn_analyzer", smart_turn_stop_seconds=3)
    assert (custom["turn_stop_strategy"], custom["smart_turn_stop_secs"]) == ("turn_analyzer", 3.0)
    # Settings saved before presets existed keep their explicit values.
    legacy = compiled(turn_start="default", turn_stop="turn_analyzer", idle_timeout_seconds=20)
    assert (legacy["turn_start_strategy"], legacy["max_user_idle_timeout"]) == ("default", 20.0)
    assert CallSettings.model_validate({"idle_timeout_seconds": 20}).response_speed == "custom"


def test_prompt_budget_warnings() -> None:
    start, end, path = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    flow = FlowDefinition.model_validate(
        {
            "nodes": [
                {
                    "id": start,
                    "kind": "start",
                    "name": "Start",
                    "greeting": "Hi",
                    "prompt": "word " * 1300,
                },  # 6500 chars ~ 1625 tokens
                {"id": end, "kind": "end", "name": "End", "prompt": "Say goodbye."},
            ],
            "paths": [
                {"id": path, "from_node": start, "to_node": end, "name": "Done", "when": "finished"}
            ],
        }
    )
    result = validate_flow(flow, global_rules="rule " * 700)  # 3500 chars ~ 875 tokens
    assert result.valid, "budget warnings never block publishing"
    by_code = {w.code: w for w in result.warnings if w.code}
    assert by_code["prompt_long"].id == start and by_code["prompt_long"].budget == 1500
    assert by_code["prompt_long"].estimated_tokens == 1625
    rules = by_code["global_rules_long"]
    assert (rules.target, rules.field, rules.budget, rules.estimated_tokens) == (
        "flow",
        "global_rules",
        800,
        875,
    )
    assert not [w for w in validate_flow(flow).warnings if w.code == "global_rules_long"]


# ------------------------------------------------------------------ calls: sync and read


@pytest.fixture
def runtime() -> Iterator[CallsRuntime]:
    yield from make_runtime()


async def test_live_call_sync_stores_latency_and_serves_it(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_live_calls(monkeypatch)
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call = await start_call(client, owner, lead["id"], agent["id"], live=True)
    run_id = max(runtime.runs)
    runtime.finish(run_id)
    runtime.runs[run_id]["logs"] = {"realtime_feedback_events": conversation(), "other": 1}
    async with get_sessionmaker()() as db:
        record = await db.get(Call, uuid.UUID(call["id"]))
        assert record is not None
        await call_service.sync_from_runtime(db, record, source="runtime")
        await db.commit()

    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    summary = detail["latency_summary"]
    assert summary["source"] == "runtime" and summary["response_p50_ms"] == 900
    assert summary["interruptions"] == 1 and summary["slow_turns"] == 1

    r = await client.get(f"/api/v1/calls/{call['id']}/latency", headers=owner.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["summary"] == summary and len(body["turns"]) == 4
    assert body["thresholds"] == {"target_ms": 800, "slow_ms": 1500}
    assert body["turns"][2]["tools"][0]["kind"] == "knowledge"

    events = (await client.get(f"/api/v1/calls/{call['id']}/events", headers=owner.headers)).json()
    applied = next(e for e in events if e["type"] == "result_applied")
    assert applied["data"]["latency_measured"] is True

    # Tenancy: another organization cannot read it.
    other = await signup(client, "other@example.com", org="Other")
    r = await client.get(f"/api/v1/calls/{call['id']}/latency", headers=other.headers)
    assert r.status_code == 404


async def test_call_without_event_log_has_no_latency(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    enable_live_calls(monkeypatch)
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    call = await start_call(client, owner, lead["id"], agent["id"], live=True)
    runtime.finish(max(runtime.runs))  # logs stays None, as on runs without events
    async with get_sessionmaker()() as db:
        record = await db.get(Call, uuid.UUID(call["id"]))
        assert record is not None
        await call_service.sync_from_runtime(db, record, source="runtime")
        await db.commit()
    detail = (await client.get(f"/api/v1/calls/{call['id']}", headers=owner.headers)).json()
    assert detail["state"] == "completed" and detail["latency_summary"] is None
    r = await client.get(f"/api/v1/calls/{call['id']}/latency", headers=owner.headers)
    assert r.status_code == 404


async def test_simulated_call_gets_labelled_synthetic_latency(
    client: httpx.AsyncClient,
    owner: Account,
    runtime: CallsRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = await published_agent(client, owner)
    lead = await create_lead(client, owner)
    result = SimulatedResult(answered=True, outcome="qualified", duration_seconds=60)
    monkeypatch.setattr(call_service, "simulate", lambda **_: result)
    call = await start_call(client, owner, lead["id"], agent["id"])
    assert await complete_simulated_call({}, call["id"]) == "completed"
    body = (await client.get(f"/api/v1/calls/{call['id']}/latency", headers=owner.headers)).json()
    assert body["summary"]["source"] == "simulated" and body["summary"]["turns"] == 4
    again = latency.simulated(call["id"], duration_seconds=60, started_at=T0)
    assert again["summary"]["response_p50_ms"] == body["summary"]["response_p50_ms"]


# ------------------------------------------------------------------ analytics


async def test_overview_counts_real_calls_only(client: httpx.AsyncClient, owner: Account) -> None:
    org = uuid.UUID(owner.org_id)
    real_a = latency.parse_events(conversation())  # responses 620, 900, 2100
    real_b = {
        "summary": {**real_a["summary"], "measured_turns": 1},  # type: ignore[index]
        "turns": [{"turn": 1, "response_ms": 400}],
    }
    fake = latency.simulated("x", duration_seconds=60, started_at=T0)
    fake["summary"]["source"] = "simulated"
    async with get_sessionmaker()() as db, db.begin():
        agent = Agent(
            organization_id=org,
            name="Q",
            direction=AgentDirection.OUTBOUND,
            status=AgentStatus.ACTIVE,
        )
        db.add(agent)
        await db.flush()
        base = {
            "organization_id": org,
            "direction": CallDirection.OUTBOUND,
            "agent_id": agent.id,
            "state": CallState.COMPLETED,
        }
        db.add_all(
            [
                Call(**base, mode=CallMode.LIVE, latency=real_a),
                Call(**base, mode=CallMode.LIVE, latency=real_b),
                Call(**base, mode=CallMode.LIVE, latency=None),
                Call(**base, mode=CallMode.LIVE, latency={"summary": "bad", "turns": "bad"}),
                # Dry runs never count, even if mislabelled as runtime.
                Call(**base, mode=CallMode.SIMULATED, latency=fake),
                Call(**base, mode=CallMode.SIMULATED, latency=real_a),
            ]
        )
    o = (await client.get("/api/v1/analytics/calls-overview", headers=owner.headers)).json()
    assert o["measured_calls"] == 2
    assert o["response_p50_ms"] == 760  # samples 400, 620, 900, 2100
    assert o["response_p95_ms"] == 1920
    assert o["by_agent"][0]["response_p50_ms"] == 760

    other = await signup(client, "other@example.com", org="Other")
    o2 = (await client.get("/api/v1/analytics/calls-overview", headers=other.headers)).json()
    assert o2["measured_calls"] == 0 and o2["response_p50_ms"] is None


# ------------------------------------------------------------------ AI settings: guide & presets


async def _connect(client: httpx.AsyncClient, acct: Account, provider: str, **extra: Any) -> str:
    body = {"name": provider, "provider": provider, "credentials": {"api_key": VALID_KEY}, **extra}
    r = await client.post("/api/v1/ai/connections", headers=acct.headers, json=body)
    assert r.status_code == 201, r.text
    return str(r.json()["id"])


async def test_latency_guide_is_honest(client: httpx.AsyncClient, owner: Account) -> None:
    r = await client.get("/api/v1/ai-settings/latency-guide", headers=owner.headers)
    assert r.status_code == 200, r.text
    guide = r.json()
    assert "not measured by ATLAS" in guide["disclaimer"]
    assert {p["id"] for p in guide["presets"]} >= {"fast_cloud", "balanced_cloud"}
    assert all(p["kind"] in ("stt", "llm", "tts") for p in guide["providers"])
    speaches = next(
        p for p in guide["providers"] if p["provider"] == "speaches" and p["kind"] == "stt"
    )
    assert speaches["streaming"] is False and speaches["typical_ms"] is None
    text = str(guide).lower()
    assert "dograh" not in text and "pipecat" not in text
    viewer = await invite_and_join(client, owner, "viewer@example.com", "viewer")
    r = await client.get("/api/v1/ai-settings/latency-guide", headers=viewer.headers)
    assert r.status_code == 403


async def test_apply_preset(
    client: httpx.AsyncClient, owner: Account, runtime: CallsRuntime
) -> None:
    url = "/api/v1/ai-settings/presets/balanced_cloud/apply"
    await _connect(client, owner, "openai")
    r = await client.post(url, headers=owner.headers)
    assert r.status_code == 409, r.text
    assert r.json()["error"]["details"] == {"missing": ["deepgram", "elevenlabs"]}

    stt = await _connect(client, owner, "deepgram")
    tts = await _connect(client, owner, "elevenlabs")
    r = await client.post(url, headers=owner.headers)
    assert r.status_code == 200, r.text
    selection = r.json()
    assert selection["stt"] == {"connection_id": stt, "settings": {"model": "nova-3-general"}}
    assert selection["tts"]["connection_id"] == tts
    assert selection["llm"]["settings"] == {"model": "gpt-4.1-mini"}

    assert (
        await client.post("/api/v1/ai-settings/presets/nope/apply", headers=owner.headers)
    ).status_code == 404
    manager = await invite_and_join(client, owner, "manager@example.com", "manager")
    assert (await client.post(url, headers=manager.headers)).status_code == 403


def test_preset_picks_matching_self_hosted_connection() -> None:
    from app.schemas.voice import ConnectionOut

    def conn(id_: str, name: str, url: str) -> ConnectionOut:
        return ConnectionOut(
            id=id_,
            name=name,
            provider="speaches",
            provider_title="Self-hosted",
            connection_settings={"base_url": url},
            configured_credentials=[],
        )

    llm = conn("a", "Local LLM", "http://local-llm:11434/v1")
    speech = conn("b", "Local speech", "http://local-speech:8000/v1")
    assert ai_speed._pick("llm", "speaches", [speech, llm]) is llm
    assert ai_speed._pick("stt", "speaches", [llm, speech]) is speech
    assert ai_speed._pick("tts", "speaches", [llm, speech]) is speech
    assert ai_speed._pick("tts", "cartesia", [llm, speech]) is None
    pocket = conn("c", "Pocket TTS", "http://local-tts:8000/v1")
    required = ai_speed._REQUIRED_HINTS[("private_pocket_tts", "tts")]
    assert ai_speed._pick("tts", "speaches", [llm, speech, pocket], required) is pocket
    assert ai_speed._pick("tts", "speaches", [llm, speech], required) is None
    assert ai_speed._pick("tts", "speaches", [pocket, speech]) is speech, "Kokoro by default"
    guide = ai_speed.latency_guide()
    assert "kokoro" not in str(guide.model_dump()).replace("Kokoro-82M", "")
    preset = next(p for p in guide.presets if p.id == "private_pocket_tts")
    assert preset.choices.tts.settings == {"model": "pocket-tts", "voice": "alba"}


# ------------------------------------------------------------------ pipeline health

METRICS = """\
# HELP dograh_ai_latency_seconds Pipecat service response latency
# TYPE dograh_ai_latency_seconds histogram
dograh_ai_latency_seconds_bucket{conversation_type="voice",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="0.2"} 2.0
dograh_ai_latency_seconds_bucket{conversation_type="voice",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="0.5"} 6.0
dograh_ai_latency_seconds_bucket{conversation_type="voice",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="1.0"} 8.0
dograh_ai_latency_seconds_bucket{conversation_type="voice",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="+Inf"} 8.0
dograh_ai_latency_seconds_count{conversation_type="voice",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService"} 8.0
dograh_ai_latency_seconds_sum{conversation_type="voice",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService"} 3.1
dograh_ai_latency_seconds_bucket{conversation_type="text",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="0.2"} 2.0
dograh_ai_latency_seconds_bucket{conversation_type="text",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="0.5"} 2.0
dograh_ai_latency_seconds_bucket{conversation_type="text",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="1.0"} 2.0
dograh_ai_latency_seconds_bucket{conversation_type="text",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService",le="+Inf"} 2.0
dograh_ai_latency_seconds_count{conversation_type="text",kind="llm",model="gpt-4.1-mini",service="OpenAILLMService"} 2.0
dograh_ai_latency_seconds_bucket{conversation_type="voice",kind="stt",model="nova-3",service="DeepgramSTTService",le="0.2"} 0.0
dograh_ai_latency_seconds_bucket{conversation_type="voice",kind="stt",model="nova-3",service="DeepgramSTTService",le="+Inf"} 0.0
dograh_ai_latency_seconds_count{conversation_type="voice",kind="stt",model="nova-3",service="DeepgramSTTService"} 0.0
# TYPE dograh_response_latency_seconds histogram
dograh_response_latency_seconds_bucket{kind="user_to_bot",le="0.5"} 0.0
dograh_response_latency_seconds_bucket{kind="user_to_bot",le="1.0"} 4.0
dograh_response_latency_seconds_bucket{kind="user_to_bot",le="2.0"} 5.0
dograh_response_latency_seconds_bucket{kind="user_to_bot",le="+Inf"} 6.0
dograh_response_latency_seconds_count{kind="user_to_bot"} 6.0
dograh_active_calls 1.0
garbage line {{{
"""  # noqa: E501


def test_histogram_quantile_interpolates_like_promql() -> None:
    buckets = {0.2: 2.0, 0.5: 6.0, 1.0: 8.0, float("inf"): 8.0}
    assert ai_speed.histogram_quantile(0.5, buckets) == pytest.approx(0.35)
    assert ai_speed.histogram_quantile(0.0, {}) is None
    assert ai_speed.histogram_quantile(0.5, {0.2: 0.0, float("inf"): 0.0}) is None
    # Rank inside +Inf returns the highest finite bound.
    assert (
        ai_speed.histogram_quantile(0.95, {0.5: 0.0, 1.0: 4.0, 2.0: 5.0, float("inf"): 6.0}) == 2.0
    )


def test_pipeline_health_parses_metrics() -> None:
    health = ai_speed.health_from_text(METRICS)
    assert health.available and health.scope == "platform" and health.window == "process lifetime"
    assert [s.service for s in health.services] == ["OpenAILLMService"], "empty series dropped"
    llm = health.services[0]
    # voice + text series summed: buckets 4/8/10 of 10; rank 5 -> 0.2 + 0.3 * 1/4
    assert (llm.kind, llm.samples, llm.p50_ms) == ("llm", 10, 275)
    assert llm.p95_ms == 875
    assert health.response is not None
    assert (health.response.samples, health.response.p50_ms, health.response.p95_ms) == (
        6,
        875,
        2000,
    )


async def test_pipeline_health_endpoint(
    client: httpx.AsyncClient, owner: Account, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "/api/v1/ai-settings/pipeline-health"
    r = await client.get(url, headers=owner.headers)
    assert r.status_code == 200 and r.json()["available"] is False, "secret not configured"

    from pydantic import SecretStr

    monkeypatch.setattr(get_settings(), "voice_runtime_metrics_secret", SecretStr("s3cret"))
    seen: list[str | None] = []

    def metrics(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-dograh-devops-secret"))
        return httpx.Response(200, text=METRICS)

    with respx.mock(assert_all_called=False) as router:
        router.get("http://runtime.test/api/v1/metrics").mock(side_effect=metrics)
        r = await client.get(url, headers=owner.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["available"] is True and body["response"]["p50_ms"] == 875
    assert "Platform-wide" in body["note"] and seen == ["s3cret"]

    with respx.mock(assert_all_called=False) as router:
        router.get("http://runtime.test/api/v1/metrics").mock(return_value=httpx.Response(503))
        assert (await client.get(url, headers=owner.headers)).json()["available"] is False
    with respx.mock(assert_all_called=False) as router:
        router.get("http://runtime.test/api/v1/metrics").mock(side_effect=httpx.ConnectError("x"))
        assert (await client.get(url, headers=owner.headers)).json()["available"] is False

    manager = await invite_and_join(client, owner, "manager@example.com", "manager")
    assert (await client.get(url, headers=manager.headers)).status_code == 403
