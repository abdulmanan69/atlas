"""Per-turn response times from the voice service's call event log (ADR 0006). Pure, no I/O.

The runtime saves every realtime feedback event of a call in the run's
``logs["realtime_feedback_events"]`` when the call ends
(``services/pipecat/event_handlers.py:524-528``; returned by
``GET /workflow/{id}/runs/{run_id}``, ``routes/workflow.py:1502``). Each event is
``{type, timestamp, turn?, node_id?, node_name?, payload}`` with an ISO-8601 UTC
timestamp in milliseconds, sorted by timestamp
(``realtime_feedback_events.py:154-180``, ``in_memory_buffers.py:150-185``).

Everything here is defensive: the log is external input, so malformed events are
skipped and the parser never raises. ``parse_events`` returns None when the log holds
nothing that describes a conversation.
"""

from __future__ import annotations

import math
import random
import re
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.schemas.latency import SLOW_MS

# Event types (pipecat.utils.enums.RealtimeFeedbackType, read from the running image).
USER_TRANSCRIPTION = "rtf-user-transcription"
BOT_TEXT = "rtf-bot-text"
FUNCTION_CALL_START = "rtf-function-call-start"
FUNCTION_CALL_END = "rtf-function-call-end"
TTFB_METRIC = "rtf-ttfb-metric"
LATENCY_MEASURED = "rtf-latency-measured"
BOT_STARTED_SPEAKING = "rtf-bot-started-speaking"
BOT_STOPPED_SPEAKING = "rtf-bot-stopped-speaking"

# The runtime's built-in knowledge tool (tools/knowledge_base.py:349); every other
# function is an ATLAS action (or a flow path, which the runtime does not log as a call).
KNOWLEDGE_TOOL = "retrieve_from_knowledge_base"

MAX_TURNS = 300
MAX_TEXT = 500
MAX_TOOLS_PER_TURN = 20
# A response time outside this window is a clock or pairing artefact, not a measurement.
_MAX_PLAUSIBLE_MS = 120_000

_INSTANCE_SUFFIX = re.compile(r"#\d+$")


# ------------------------------------------------------------------ small helpers


def _ts(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp; naive values are UTC. None on anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ms(start: datetime | None, end: datetime | None) -> int | None:
    if start is None or end is None:
        return None
    value = round((end - start).total_seconds() * 1000)
    return value if 0 <= value <= _MAX_PLAUSIBLE_MS else None


def _seconds_to_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    ms = round(value * 1000)
    return ms if ms <= _MAX_PLAUSIBLE_MS else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned[:MAX_TEXT] if cleaned else None


def _turn(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def percentile(values: Iterable[int], q: float) -> int | None:
    """Linear-interpolated percentile (q in 0..1) of integer samples; None when empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo))


def ttfb_kind(payload: dict[str, Any]) -> str:
    """Which pipeline stage a ``rtf-ttfb-metric`` measured: "stt", "llm" or "tts".

    ``kind`` is set on current runs (``realtime_feedback_events.py:110-131``); older runs
    carry none and are LLM measurements. When ``kind`` is absent or unknown, the
    processor name ("<Vendor>STTService#3") is classified by its *suffix*: every STT class
    name contains "TTS" as a substring, so a substring test would misfile it
    (``observability/call_events/metrics.py:1-30``).
    """
    kind = payload.get("kind")
    if isinstance(kind, str) and kind.lower() in ("stt", "llm", "tts"):
        return kind.lower()
    processor = payload.get("processor")
    if isinstance(processor, str):
        name = _INSTANCE_SUFFIX.sub("", processor.strip()).upper()
        if name.endswith("STTSERVICE"):
            return "stt"
        if name.endswith("TTSSERVICE"):
            return "tts"
    return "llm"


# ------------------------------------------------------------------ parsing


class _Turn:
    __slots__ = (
        "agent_end",
        "agent_start",
        "agent_text",
        "measured_ms",
        "node_name",
        "number",
        "tools",
        "ttfb",
        "user_end",
        "user_start",
        "user_text",
    )

    def __init__(self, number: int) -> None:
        self.number = number
        self.node_name: str | None = None
        self.user_text: list[str] = []
        self.agent_text: list[str] = []
        self.user_start: datetime | None = None
        self.user_end: datetime | None = None
        self.agent_start: datetime | None = None
        self.agent_end: datetime | None = None
        self.measured_ms: int | None = None
        self.ttfb: dict[str, int] = {}
        self.tools: list[dict[str, Any]] = []

    def response_ms(self) -> int | None:
        """Runtime-measured value when present, else bot start - user end of this turn."""
        if self.measured_ms is not None:
            return self.measured_ms
        if self.user_text or self.user_end is not None:
            return _ms(self.user_end, self.agent_start)
        return None

    def to_dict(self) -> dict[str, Any]:
        response = self.response_ms()
        return {
            "turn": self.number,
            "node_name": self.node_name,
            "user_text": _text(" ".join(self.user_text)),
            "agent_text": _text(" ".join(self.agent_text)),
            "user_started_at": _iso(self.user_start),
            "user_ended_at": _iso(self.user_end),
            "agent_started_at": _iso(self.agent_start),
            "agent_ended_at": _iso(self.agent_end),
            "response_ms": response,
            "stt_ttfb_ms": self.ttfb.get("stt"),
            "llm_ttfb_ms": self.ttfb.get("llm"),
            "tts_ttfb_ms": self.ttfb.get("tts"),
            "tools": self.tools[:MAX_TOOLS_PER_TURN],
            "slow": response is not None and response > SLOW_MS,
        }


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat(timespec="milliseconds") if value else None


def _min(a: datetime | None, b: datetime | None) -> datetime | None:
    return b if a is None else a if b is None else min(a, b)


def _max(a: datetime | None, b: datetime | None) -> datetime | None:
    return b if a is None else a if b is None else max(a, b)


def extract_events(logs: Any) -> list[dict[str, Any]]:
    """The event list from a run's ``logs``; [] for anything unexpected."""
    if not isinstance(logs, dict):
        return []
    events = logs.get("realtime_feedback_events")
    if not isinstance(events, list):
        return []
    return [e for e in events if isinstance(e, dict) and isinstance(e.get("type"), str)]


def parse_events(events: Any) -> dict[str, Any] | None:
    """Build ``{"summary": LatencySummary, "turns": [LatencyTurn]}`` (as JSON) from events.

    Pairing rules (event shapes per ``realtime_feedback_events.py:28-131`` and
    ``transcript_log_coordinator.py:117-161, 260-280``):

    * Events are grouped by their ``turn`` stamp. ``rtf-user-transcription`` (final
      only) gives the person's speech: ``payload.timestamp`` = speech start,
      ``payload.end_timestamp`` = speech end from VAD stop. ``rtf-bot-text`` gives the
      agent's speech: ``payload.timestamp`` / ``payload.end_timestamp``.
    * Response time = ``rtf-latency-measured.payload.latency_seconds`` of the turn when
      present (the runtime's own user-silence → bot-speaking value,
      ``run_pipeline.py:1259-1280``), else agent speech start - user speech end.
    * ``rtf-ttfb-metric``: the first value per stage in a turn; see ``ttfb_kind``.
    * ``rtf-function-call-start/end`` are matched by ``tool_call_id``; duration is the
      difference of their event timestamps.
    * An interruption is a final user transcription whose speech starts while an
      earlier agent reply is still being spoken.

    Never raises; returns None when there is no conversation in the events.
    """
    try:
        return _parse(events)
    except Exception:  # pragma: no cover - defensive: external input must never break sync
        return None


def _parse(events: Any) -> dict[str, Any] | None:
    if not isinstance(events, list):
        return None
    turns: dict[int, _Turn] = {}
    tool_starts: dict[str, tuple[int, str, datetime | None]] = {}
    ttfb_all: dict[str, list[int]] = {"stt": [], "llm": [], "tts": []}
    agent_spans: list[tuple[datetime, datetime]] = []
    user_starts: list[datetime] = []
    last_turn = 0

    def turn_for(raw: Any) -> _Turn | None:
        nonlocal last_turn
        number = _turn(raw)
        if number is None:
            number = last_turn
        if number not in turns:
            if len(turns) >= MAX_TURNS:
                return None
            turns[number] = _Turn(number)
        last_turn = number
        return turns[number]

    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        at = _ts(event.get("timestamp"))

        if kind == USER_TRANSCRIPTION:
            if payload.get("final") is not True:
                continue
            text = _text(payload.get("text"))
            if text is None:
                continue
            turn = turn_for(event.get("turn"))
            if turn is None:
                continue
            start = _ts(payload.get("timestamp"))
            end = _ts(payload.get("end_timestamp"))
            turn.user_text.append(text)
            turn.user_start = _min(turn.user_start, start)
            turn.user_end = _max(turn.user_end, end)
            if start is not None:
                user_starts.append(start)
            turn.node_name = turn.node_name or _text(event.get("node_name"))
        elif kind == BOT_TEXT:
            text = _text(payload.get("text"))
            turn = turn_for(event.get("turn"))
            if turn is None:
                continue
            start = _ts(payload.get("timestamp"))
            end = _ts(payload.get("end_timestamp"))
            if text:
                turn.agent_text.append(text)
            turn.agent_start = _min(turn.agent_start, start)
            turn.agent_end = _max(turn.agent_end, end)
            if start is not None and end is not None and end > start:
                agent_spans.append((start, end))
            turn.node_name = _text(event.get("node_name")) or turn.node_name
        elif kind == LATENCY_MEASURED:
            value = _seconds_to_ms(payload.get("latency_seconds"))
            turn = turn_for(event.get("turn"))
            if value is not None and turn is not None and turn.measured_ms is None:
                turn.measured_ms = value
        elif kind == TTFB_METRIC:
            value = _seconds_to_ms(payload.get("ttfb_seconds"))
            if value is None:
                continue
            stage = ttfb_kind(payload)
            ttfb_all[stage].append(value)
            turn = turn_for(event.get("turn"))
            if turn is not None:
                turn.ttfb.setdefault(stage, value)
        elif kind == FUNCTION_CALL_START:
            name = _text(payload.get("function_name"))
            call_id = payload.get("tool_call_id")
            turn = turn_for(event.get("turn"))
            if name is None or turn is None:
                continue
            if isinstance(call_id, str) and call_id:
                tool_starts[call_id] = (turn.number, name, at)
            else:
                turn.tools.append(_tool(name, None))
        elif kind == FUNCTION_CALL_END:
            call_id = payload.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in tool_starts:
                continue
            number, name, started = tool_starts.pop(call_id)
            turns[number].tools.append(_tool(name, _ms(started, at)))

    # Calls still open when the log was saved (e.g. the call hung up mid-lookup).
    for number, name, _started in tool_starts.values():
        turns[number].tools.append(_tool(name, None))

    ordered = [turns[n] for n in sorted(turns)]
    if not any(t.user_text or t.agent_text for t in ordered):
        return None
    turn_dicts = [t.to_dict() for t in ordered]
    responses = [t["response_ms"] for t in turn_dicts if t["response_ms"] is not None]
    interruptions = sum(
        1 for s in user_starts if any(a_start < s < a_end for a_start, a_end in agent_spans)
    )
    summary = {
        "source": "runtime",
        "turns": len(turn_dicts),
        "measured_turns": len(responses),
        "response_p50_ms": percentile(responses, 0.5),
        "response_p95_ms": percentile(responses, 0.95),
        "response_max_ms": max(responses) if responses else None,
        "first_response_ms": responses[0] if responses else None,
        "stt_ttfb_p50_ms": percentile(ttfb_all["stt"], 0.5),
        "llm_ttfb_p50_ms": percentile(ttfb_all["llm"], 0.5),
        "tts_ttfb_p50_ms": percentile(ttfb_all["tts"], 0.5),
        "tool_time_ms": sum(tool["duration_ms"] or 0 for t in turn_dicts for tool in t["tools"]),
        "slow_turns": sum(1 for t in turn_dicts if t["slow"]),
        "interruptions": interruptions,
    }
    return {"summary": summary, "turns": turn_dicts}


def _tool(name: str, duration_ms: int | None) -> dict[str, Any]:
    return {
        "name": name[:120],
        "kind": "knowledge" if name == KNOWLEDGE_TOOL else "action",
        "duration_ms": duration_ms,
    }


def from_run_logs(logs: Any) -> dict[str, Any] | None:
    """Latency for a run detail's ``logs`` field; None when nothing was measured."""
    return parse_events(extract_events(logs))


# ------------------------------------------------------------------ simulated calls


def simulated(seed: str, *, duration_seconds: int, started_at: datetime) -> dict[str, Any]:
    """Synthetic, deterministic latency for a dry run, labelled ``source: "simulated"``.

    Never mixed into real averages: analytics filter on ``source == "runtime"``.
    """
    rng = random.Random(f"latency:{seed}")  # noqa: S311 - not security related
    count = max(2, min(12, duration_seconds // 15))
    turns: list[dict[str, Any]] = []
    clock = started_at
    for number in range(count):
        stt = rng.randint(120, 320)
        llm = rng.randint(250, 900)
        tts = rng.randint(90, 300)
        response = stt + llm + tts + rng.randint(40, 160)
        if number == 0:
            # The greeting: the agent speaks first, nobody is answered.
            agent_start = clock + timedelta(milliseconds=rng.randint(600, 1200))
            agent_end = agent_start + timedelta(seconds=rng.uniform(2, 4))
            turns.append(
                _sim_turn(number, None, None, agent_start, agent_end, None, None, None, None)
            )
            clock = agent_end
            continue
        user_start = clock + timedelta(milliseconds=rng.randint(300, 900))
        user_end = user_start + timedelta(seconds=rng.uniform(1, 4))
        agent_start = user_end + timedelta(milliseconds=response)
        agent_end = agent_start + timedelta(seconds=rng.uniform(2, 6))
        turns.append(
            _sim_turn(number, user_start, user_end, agent_start, agent_end, response, stt, llm, tts)
        )
        clock = agent_end
    responses = [t["response_ms"] for t in turns if t["response_ms"] is not None]
    summary = {
        "source": "simulated",
        "turns": len(turns),
        "measured_turns": len(responses),
        "response_p50_ms": percentile(responses, 0.5),
        "response_p95_ms": percentile(responses, 0.95),
        "response_max_ms": max(responses) if responses else None,
        "first_response_ms": responses[0] if responses else None,
        "stt_ttfb_p50_ms": percentile([t["stt_ttfb_ms"] for t in turns if t["stt_ttfb_ms"]], 0.5),
        "llm_ttfb_p50_ms": percentile([t["llm_ttfb_ms"] for t in turns if t["llm_ttfb_ms"]], 0.5),
        "tts_ttfb_p50_ms": percentile([t["tts_ttfb_ms"] for t in turns if t["tts_ttfb_ms"]], 0.5),
        "tool_time_ms": 0,
        "slow_turns": sum(1 for t in turns if t["slow"]),
        "interruptions": 0,
    }
    return {"summary": summary, "turns": turns}


def _sim_turn(
    number: int,
    user_start: datetime | None,
    user_end: datetime | None,
    agent_start: datetime,
    agent_end: datetime,
    response: int | None,
    stt: int | None,
    llm: int | None,
    tts: int | None,
) -> dict[str, Any]:
    return {
        "turn": number,
        "node_name": None,
        "user_text": "(synthetic reply)" if user_start else None,
        "agent_text": "(simulated agent reply)",
        "user_started_at": _iso(user_start),
        "user_ended_at": _iso(user_end),
        "agent_started_at": _iso(agent_start),
        "agent_ended_at": _iso(agent_end),
        "response_ms": response,
        "stt_ttfb_ms": stt,
        "llm_ttfb_ms": llm,
        "tts_ttfb_ms": tts,
        "tools": [],
        "slow": response is not None and response > SLOW_MS,
    }
