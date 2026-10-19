"""Compile an ATLAS agent configuration into a runtime workflow definition.

Both authoring modes share one compiler (ADR 0005): a guided agent is first turned into
a two-node flow (`guided_to_flow`), then every flow compiles to the runtime graph:

    Start → startCall, Step → agentNode, End → endCall, Path → edge,
    Global rules → globalNode, plus the trigger node that lets calls be started over
    the API and the webhook node that reports call completion back to ATLAS.

Runtime ids are deterministic (`n1..`, `p1..` by sorted ATLAS id) and the compiled
result keeps the map so the runtime's validation errors can be translated back to
ATLAS node/path ids and field names.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from app.schemas.voice import (
    AgentConfig,
    CaptureField,
    FlowDefinition,
    FlowIssue,
    FlowNode,
    FlowPath,
    FlowPosition,
)

LANGUAGE_NAMES = {
    "en": "English",
    "ur": "Urdu",
    "hi": "Hindi",
    "ar": "Arabic",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "tr": "Turkish",
}

# Variables ATLAS fills from the CRM for every call.
LEAD_VARIABLES = ("first_name", "last_name", "full_name", "company", "job_title", "email")
OPT_OUT_FIELD = "opted_out"
WEBHOOK_SECRET_HEADER = "X-Atlas-Webhook-Secret"  # noqa: S105 - a header name
EXTRACTION_PROMPT = (
    "Extract the requested details from the conversation. "
    "Leave a value empty when it was not discussed."
)

# Fixed ids for the nodes ATLAS injects around every flow.
TRIGGER_ID = "trigger"
REPORT_ID = "report"
GLOBAL_ID = "global"

# Stable ids for the flow a guided agent compiles to, so conversion is idempotent.
_GUIDED_NS = uuid.UUID("7b1c7d7e-5f3a-4f7e-9d2b-3c1e6a4d8f01")
GUIDED_START_ID = str(uuid.uuid5(_GUIDED_NS, "start"))
GUIDED_END_ID = str(uuid.uuid5(_GUIDED_NS, "end"))
GUIDED_PATH_ID = str(uuid.uuid5(_GUIDED_NS, "wrap-up"))

# Runtime field → ATLAS field, per node kind and for paths.
_NODE_FIELDS = {
    "prompt": "prompt",
    "greeting": "greeting",
    "name": "name",
    "allow_interrupt": "allow_interrupt",
    "allow_greeting_interrupt": "allow_interrupt",
    "add_global_prompt": "use_global_rules",
    "extraction_variables": "capture_fields",
    "extraction_prompt": "capture_fields",
    "tool_uuids": "action_ids",
    "document_uuids": "knowledge_ids",
}
_PATH_FIELDS = {
    "label": "name",
    "condition": "when",
    "transition_speech": "say_while_moving",
    "source": "from_node",
    "target": "to_node",
}


@dataclass(frozen=True, slots=True)
class WebhookTarget:
    url: str
    credential_uuid: str


# ------------------------------------------------------------------ guided mode


def compile_prompt(cfg: AgentConfig) -> str:
    """The Start prompt of a guided agent: persona, instructions, questions and rules."""
    language = LANGUAGE_NAMES.get(cfg.language.split("-")[0], cfg.language)
    parts = [
        f"You are {cfg.persona_name}, speaking on a phone call on behalf of {cfg.company_name}.",
        cfg.instructions.strip(),
    ]
    if cfg.qualification_questions:
        questions = "\n".join(f"{i}. {q}" for i, q in enumerate(cfg.qualification_questions, 1))
        parts.append(
            "## Questions to cover\nAsk these naturally, one at a time, and listen to each "
            f"answer before moving on:\n{questions}"
        )
    rules = [
        "Keep each reply short (one or two sentences): this is a live phone call.",
        f"Speak in {language} unless the person clearly prefers another language.",
        "Never invent prices, availability, policies or facts that are not in these instructions.",
        "If the person asks not to be contacted again, apologise, confirm they will not be "
        "called again, and end the call politely.",
    ]
    if cfg.ai_disclosure:
        rules.append("If asked whether you are a person or a machine, say you are an AI assistant.")
    parts.append("## Rules\n" + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(p for p in parts if p)


def guided_to_flow(cfg: AgentConfig) -> FlowDefinition:
    """The two-node flow a guided agent is equivalent to. Lossless: nothing is dropped.

    Start carries the greeting and the compiled prompt; the single End carries the
    closing and the captured details; one path named "Wrap up" is taken when the
    guided end condition holds.
    """
    return FlowDefinition(
        nodes=[
            FlowNode(
                id=GUIDED_START_ID,
                kind="start",
                name="Conversation",
                position=FlowPosition(x=0, y=0),
                prompt=compile_prompt(cfg),
                greeting=cfg.greeting,
                allow_interrupt=cfg.allow_interrupt,
                use_global_rules=True,
            ),
            FlowNode(
                id=GUIDED_END_ID,
                kind="end",
                name="Wrap up",
                position=FlowPosition(x=700, y=0),
                prompt=cfg.closing,
                allow_interrupt=cfg.allow_interrupt,
                capture_fields=list(cfg.capture_fields),
            ),
        ],
        paths=[
            FlowPath(
                id=GUIDED_PATH_ID,
                from_node=GUIDED_START_ID,
                to_node=GUIDED_END_ID,
                name="Wrap up",
                when=cfg.end_condition,
            )
        ],
    )


def effective_flow(cfg: AgentConfig) -> FlowDefinition:
    """The flow that will be published, whichever way the agent was authored."""
    if cfg.mode == "flow" and cfg.flow is not None:
        return cfg.flow
    return guided_to_flow(cfg)


# ------------------------------------------------------------------ flow → runtime graph


def _extraction(fields: list[CaptureField], *, with_opt_out: bool) -> dict[str, Any]:
    variables = [{"name": f.name, "type": f.type, "prompt": f.description} for f in fields]
    if with_opt_out:
        variables.append(
            {
                "name": OPT_OUT_FIELD,
                "type": "boolean",
                "prompt": "Did the person ask not to be called or contacted again?",
            }
        )
    if not variables:
        return {}
    return {
        "extraction_enabled": True,
        "extraction_prompt": EXTRACTION_PROMPT,
        "extraction_variables": variables,
    }


def _refs(
    node: FlowNode,
    tool_uuids: Mapping[str, str] | None,
    document_uuids: Mapping[str, str] | None,
) -> dict[str, Any]:
    """Runtime tool/document references for a node. Unknown ids are left out."""
    data: dict[str, Any] = {}
    if node.action_ids and tool_uuids is not None:
        tools = [tool_uuids[a] for a in node.action_ids if a in tool_uuids]
        if tools:
            data["tool_uuids"] = tools
    if node.knowledge_ids and document_uuids is not None:
        docs = [document_uuids[k] for k in node.knowledge_ids if k in document_uuids]
        if docs:
            data["document_uuids"] = docs
    return data


def _node_data(
    node: FlowNode,
    tool_uuids: Mapping[str, str] | None,
    document_uuids: Mapping[str, str] | None,
) -> tuple[str, dict[str, Any]]:
    if node.kind == "start":
        return "startCall", {
            "name": node.name,
            "prompt": node.prompt,
            "greeting_type": "text",
            "greeting": node.greeting or "",
            "allow_interrupt": node.allow_interrupt,
            "allow_greeting_interrupt": node.allow_interrupt,
            "add_global_prompt": node.use_global_rules,
            **_extraction(node.capture_fields, with_opt_out=False),
            **_refs(node, tool_uuids, document_uuids),
        }
    if node.kind == "step":
        return "agentNode", {
            "name": node.name,
            "prompt": node.prompt,
            "allow_interrupt": node.allow_interrupt,
            "add_global_prompt": node.use_global_rules,
            **_extraction(node.capture_fields, with_opt_out=False),
            **_refs(node, tool_uuids, document_uuids),
        }
    # End: one closing reply, then hang up. Global rules are not prepended here and the
    # opt-out flag is extracted on every End so do-not-call requests are always honoured.
    return "endCall", {
        "name": node.name,
        "prompt": node.prompt,
        "allow_interrupt": node.allow_interrupt,
        "add_global_prompt": False,
        **_extraction(node.capture_fields, with_opt_out=True),
    }


def _edge_data(path: FlowPath) -> dict[str, Any]:
    data: dict[str, Any] = {"label": path.name, "condition": path.when}
    speech = (path.say_while_moving or "").strip()
    if speech:
        data["transition_speech"] = speech
        data["transition_speech_type"] = "text"
    return data


def _webhook_node(webhook: WebhookTarget, position: dict[str, float]) -> dict[str, Any]:
    return {
        "id": REPORT_ID,
        "type": "webhook",
        "position": position,
        "data": {
            "name": "Report call to CRM",
            "enabled": True,
            "http_method": "POST",
            "endpoint_url": webhook.url,
            "credential_uuid": webhook.credential_uuid,
            "custom_headers": [],
            # Routing fields only; results are always re-fetched (ADR 0003).
            # Each placeholder is verified against the runtime's render context
            # (tasks/run_integrations.py, `_build_render_context`): top-level
            # workflow_run_id / workflow_id and the run's initial_context, where
            # telephony stamps direction ("inbound" | "outbound"). Missing values
            # render as "" (e.g. atlas_call_id on an inbound call).
            "payload_template": {
                "atlas_call_id": "{{initial_context.atlas_call_id}}",
                "workflow_run_id": "{{workflow_run_id}}",
                "workflow_id": "{{workflow_id}}",
                "direction": "{{initial_context.direction}}",
            },
        },
    }


@dataclass(frozen=True, slots=True)
class CompiledWorkflow:
    """A runtime definition plus the id maps needed to explain its errors in ATLAS terms."""

    definition: dict[str, Any]
    node_ids: dict[str, str] = field(default_factory=dict)  # ATLAS node id → runtime id
    path_ids: dict[str, str] = field(default_factory=dict)  # ATLAS path id → runtime id

    def translate(self, error: Mapping[str, Any]) -> FlowIssue:
        """Map a runtime `{kind, id, field, message}` error to an ATLAS node/path issue."""
        kind = str(error.get("kind") or "workflow")
        runtime_id = str(error.get("id") or "")
        raw_field = str(error.get("field") or "")
        leaf = raw_field.removeprefix("data.").split(".")[0]
        message = str(error.get("message") or "Invalid value")
        if kind == "node":
            if runtime_id == GLOBAL_ID:
                return FlowIssue(target="flow", id="", field="global_rules", message=message)
            for atlas_id, rid in self.node_ids.items():
                if rid == runtime_id:
                    mapped = _NODE_FIELDS.get(leaf, leaf or None)
                    return FlowIssue(target="node", id=atlas_id, field=mapped, message=message)
            if runtime_id in (TRIGGER_ID, REPORT_ID):
                return FlowIssue(target="flow", id="", field="call_reporting", message=message)
        if kind == "edge":
            for atlas_id, rid in self.path_ids.items():
                if rid == runtime_id:
                    mapped = _PATH_FIELDS.get(leaf, leaf or None)
                    return FlowIssue(target="path", id=atlas_id, field=mapped, message=message)
        return FlowIssue(target="flow", id="", field=leaf or None, message=message)


def compile_workflow(
    cfg: AgentConfig,
    *,
    webhook: WebhookTarget | None,
    tool_uuids: Mapping[str, str] | None = None,
    document_uuids: Mapping[str, str] | None = None,
) -> CompiledWorkflow:
    """Compile the agent's effective flow into the runtime graph.

    `tool_uuids` / `document_uuids` map ATLAS action and document ids to the runtime's
    uuids; when None, references are not emitted at all.
    """
    flow = effective_flow(cfg)
    node_ids = {n.id: f"n{i}" for i, n in enumerate(sorted(flow.nodes, key=lambda n: n.id), 1)}
    path_ids = {p.id: f"p{i}" for i, p in enumerate(sorted(flow.paths, key=lambda p: p.id), 1)}

    start = next((n for n in flow.nodes if n.kind == "start"), None)
    origin = start.position if start else FlowPosition()
    xs = [n.position.x for n in flow.nodes] or [0.0]

    nodes: list[dict[str, Any]] = [
        {
            "id": TRIGGER_ID,
            "type": "trigger",
            "position": {"x": origin.x - 420, "y": origin.y},
            "data": {"name": "API trigger", "enabled": True},
        }
    ]
    rules = (cfg.global_rules or "").strip()
    if rules:
        nodes.append(
            {
                "id": GLOBAL_ID,
                "type": "globalNode",
                "position": {"x": origin.x - 420, "y": origin.y + 200},
                "data": {"name": "Global rules", "prompt": rules},
            }
        )
    for node in flow.nodes:
        node_type, data = _node_data(node, tool_uuids, document_uuids)
        nodes.append(
            {
                "id": node_ids[node.id],
                "type": node_type,
                "position": {"x": node.position.x, "y": node.position.y},
                "data": data,
            }
        )
    if webhook is not None:
        nodes.append(_webhook_node(webhook, {"x": max(xs) + 420, "y": origin.y}))

    edges = [
        {
            "id": path_ids[path.id],
            "source": node_ids.get(path.from_node, path.from_node),
            "target": node_ids.get(path.to_node, path.to_node),
            "data": _edge_data(path),
        }
        for path in flow.paths
    ]
    return CompiledWorkflow(
        definition={"nodes": nodes, "edges": edges}, node_ids=node_ids, path_ids=path_ids
    )


def build_definition(cfg: AgentConfig, *, webhook: WebhookTarget | None) -> dict[str, Any]:
    """The runtime workflow definition for an agent (see `compile_workflow`)."""
    return compile_workflow(cfg, webhook=webhook).definition


# ------------------------------------------------------------------ configurations


def build_configurations(
    cfg: AgentConfig, *, connection_ids: set[str] | None = None
) -> dict[str, Any]:
    """Workflow-level settings: outcomes, call settings and the optional model override.

    Field names follow the runtime's `workflow_configurations` schema (ADR 0005 table).
    """
    cs = cfg.call_settings
    configurations: dict[str, Any] = {
        "call_dispositions": [{"code": o.code, "description": o.description} for o in cfg.outcomes],
        "max_call_duration": cs.max_call_seconds,
        "max_user_idle_timeout": float(cs.idle_timeout_seconds),
        "turn_start_strategy": cs.turn_start,
        "turn_start_min_words": cs.turn_start_min_words,
        "turn_stop_strategy": cs.turn_stop,
        "smart_turn_stop_secs": cs.smart_turn_stop_seconds,
        "dictionary": ", ".join(cs.key_terms),
        # ADR 0006: summarising older turns keeps long calls fast; the speech cache only
        # takes effect for TTS providers that implement it (runtime ignores it otherwise).
        "context_compaction_enabled": cs.context_compaction,
        "tts_cache_enabled": cs.tts_cache,
        # Always on: transcripts carry start-end times per line (run_pipeline.py:701-704).
        "transcript_configuration": {"include_end_timestamps": True},
        "ambient_noise_configuration": {
            "enabled": cs.ambient.enabled,
            "volume": cs.ambient.volume,
        },
        "voicemail_detection": {
            "enabled": cs.voicemail.enabled,
            "use_workflow_llm": True,
            "voicemail_action": cs.voicemail.action,
            "voicemail_message": {"text": (cs.voicemail.message or "").strip()},
        },
    }
    override = cfg.model_override
    if override and (override.llm or override.tts):
        spec: dict[str, Any] = {}
        for kind in ("llm", "tts"):
            choice = getattr(override, kind)
            if choice is None:
                continue
            if connection_ids is not None and choice.connection_id not in connection_ids:
                raise ValueError(f"Unknown {kind} connection")
            spec[kind] = {
                "provider_connection_uuid": choice.connection_id,
                "settings": {k: v for k, v in choice.settings.items() if v is not None},
            }
        configurations["model_configuration_override"] = spec
    return configurations
