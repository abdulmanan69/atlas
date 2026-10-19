"""Flow canvas (ADR 0005): validation rules, the shared compiler, and the agent endpoints."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from app.integrations.voice_runtime.workflow_builder import (
    GLOBAL_ID,
    OPT_OUT_FIELD,
    REPORT_ID,
    TRIGGER_ID,
    WebhookTarget,
    build_configurations,
    build_definition,
    compile_prompt,
    compile_workflow,
    guided_to_flow,
)
from app.schemas.voice import (
    AgentConfig,
    CallSettings,
    CaptureField,
    FlowDefinition,
    FlowNode,
    FlowPath,
    FlowValidation,
    path_function_name,
)
from app.services.agents import TEMPLATES
from app.services.flows import compose_prompt, validate_flow
from tests.conftest import Account
from tests.fake_runtime import _json
from tests.test_actions import ToolRuntime, public_dns  # noqa: F401
from tests.test_actions import create as create_action
from tests.test_knowledge import KnowledgeRuntime
from tests.test_voice import _agent, _setup_models

# ------------------------------------------------------------------ builders


def node(kind: str, name: str, **extra: Any) -> FlowNode:
    data: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "kind": kind,
        "name": name,
        "prompt": f"Do {name}.",
    }
    if kind == "start":
        data["greeting"] = "Hello there"
    data.update(extra)
    return FlowNode.model_validate(data)


def path(
    src: FlowNode, dst: FlowNode, name: str, when: str = "When it applies", **extra: Any
) -> FlowPath:
    return FlowPath(
        id=str(uuid.uuid4()), from_node=src.id, to_node=dst.id, name=name, when=when, **extra
    )


def linear() -> tuple[FlowDefinition, FlowNode, FlowNode, FlowNode]:
    start, step, end = node("start", "Open"), node("step", "Qualify"), node("end", "Done")
    flow = FlowDefinition(
        nodes=[start, step, end], paths=[path(start, step, "Continue"), path(step, end, "Finish")]
    )
    return flow, start, step, end


def branching() -> tuple[FlowDefinition, dict[str, FlowNode], dict[str, FlowPath]]:
    n = {
        "start": node(
            "start", "Open", capture_fields=[CaptureField(name="mood", description="Mood")]
        ),
        "qualify": node(
            "step", "Qualify", capture_fields=[CaptureField(name="budget", description="Budget")]
        ),
        "book": node("step", "Book"),
        "booked": node(
            "end", "Booked", capture_fields=[CaptureField(name="slot", description="Slot")]
        ),
        "later": node("end", "Later"),
    }
    p = {
        "go": path(n["start"], n["qualify"], "Ready", "They can talk"),
        "busy": path(n["start"], n["later"], "Bad time", "They cannot talk"),
        "fit": path(
            n["qualify"], n["book"], "Good fit", "They qualify", say_while_moving="One moment."
        ),
        "nofit": path(n["qualify"], n["later"], "Not a fit", "They do not qualify"),
        "done": path(n["book"], n["booked"], "Booked", "A slot was agreed"),
        "retry": path(n["book"], n["qualify"], "Rethink", "They changed their mind"),
    }
    return FlowDefinition(nodes=list(n.values()), paths=list(p.values())), n, p


def guided(**extra: Any) -> AgentConfig:
    return AgentConfig(
        company_name="Acme",
        greeting="Hi {{first_name}}",
        instructions="Qualify the lead and book a viewing when they fit.",
        capture_fields=[CaptureField(name="budget", description="Budget")],
        **extra,
    )


def flow_config(flow: FlowDefinition, **extra: Any) -> AgentConfig:
    return guided(mode="flow", flow=flow, **extra)


def issues(v: FlowValidation, kind: str = "errors") -> set[tuple[str, str, str | None]]:
    return {(i.target, i.id, i.field) for i in getattr(v, kind)}


# ------------------------------------------------------------------ validation rules


def test_valid_branching_flow_has_no_errors() -> None:
    flow, _, p = branching()
    v = validate_flow(flow)
    assert v.valid and v.errors == []
    # The Book → Qualify path closes a cycle: allowed, but worth a warning.
    assert issues(v, "warnings") == {("path", p["retry"].id, None)}


def test_exactly_one_start_and_at_least_one_end() -> None:
    end = node("end", "Done")
    none = validate_flow(FlowDefinition(nodes=[end], paths=[]))
    assert ("flow", "", None) in issues(none)
    assert any("Start" in e.message for e in none.errors)

    a, b, e = node("start", "A"), node("start", "B"), node("end", "E")
    two = validate_flow(FlowDefinition(nodes=[a, b, e], paths=[path(a, e, "x"), path(b, e, "y")]))
    assert ("node", b.id, None) in issues(two) and ("node", a.id, None) not in issues(two)

    s = node("start", "S")
    no_end = validate_flow(FlowDefinition(nodes=[s], paths=[]))
    assert any("End" in e.message for e in no_end.errors)


def test_start_has_no_incoming_and_end_has_no_outgoing() -> None:
    flow, start, step, end = linear()
    back = path(step, start, "Back")
    out = path(end, step, "Again")
    v = validate_flow(FlowDefinition(nodes=flow.nodes, paths=[*flow.paths, back, out]))
    assert ("path", back.id, "to_node") in issues(v)
    assert ("path", out.id, "from_node") in issues(v)


def test_every_non_end_node_needs_a_path_out() -> None:
    start, step, end = node("start", "S"), node("step", "Dead"), node("end", "E")
    v = validate_flow(FlowDefinition(nodes=[start, step, end], paths=[path(start, end, "Go")]))
    assert ("node", step.id, "paths") in issues(v)
    # Unreachable at the same time: both problems are reported.
    assert ("node", step.id, None) in issues(v)


def test_every_node_must_be_reachable_from_start() -> None:
    flow, start, step, end = linear()
    island = node("step", "Island")
    v = validate_flow(
        FlowDefinition(nodes=[*flow.nodes, island], paths=[*flow.paths, path(island, end, "x")])
    )
    assert issues(v) == {("node", island.id, None)}


def test_path_names_unique_per_source_after_normalisation() -> None:
    start, a, b = node("start", "S"), node("end", "A"), node("end", "B")
    p1, p2 = path(start, a, "Book Now"), path(start, b, "book-now")
    assert path_function_name("Book Now") == path_function_name("book-now") == "book_now"
    v = validate_flow(FlowDefinition(nodes=[start, a, b], paths=[p1, p2]))
    assert issues(v) == {("path", p1.id, "name"), ("path", p2.id, "name")}
    # The same name from different sources is fine.
    step = node("step", "Mid")
    ok = validate_flow(
        FlowDefinition(
            nodes=[start, step, a], paths=[path(start, step, "Next"), path(step, a, "Next")]
        )
    )
    assert ok.valid


def test_path_name_cannot_shadow_an_action_on_the_source_node() -> None:
    start, end = node("start", "S", action_ids=["act-1"]), node("end", "E")
    p = path(start, end, "Book viewing")
    v = validate_flow(FlowDefinition(nodes=[start, end], paths=[p]), {"act-1": "book_viewing"})
    assert issues(v) == {("path", p.id, "name")}
    # An action attached elsewhere does not collide.
    assert validate_flow(
        FlowDefinition(nodes=[start, end], paths=[p]), {"act-2": "book_viewing"}
    ).valid


def test_node_names_unique() -> None:
    start, a, b = node("start", "S"), node("end", "Done"), node("end", "done ")
    v = validate_flow(
        FlowDefinition(nodes=[start, a, b], paths=[path(start, a, "x"), path(start, b, "y")])
    )
    assert issues(v) == {("node", b.id, "name")}


def test_blank_text_is_reported_with_ids_and_fields() -> None:
    start = node("start", "S", prompt="  ", greeting=" ")
    end = node("end", "E", prompt="")
    p = path(start, end, "Go", when="   ")
    v = validate_flow(FlowDefinition(nodes=[start, end], paths=[p]))
    assert issues(v) == {
        ("node", start.id, "prompt"),
        ("node", start.id, "greeting"),
        ("node", end.id, "prompt"),
        ("path", p.id, "when"),
    }


def test_cycles_are_warnings_and_self_loops_named() -> None:
    flow, start, step, end = linear()
    loop = path(step, step, "Again")
    v = validate_flow(FlowDefinition(nodes=flow.nodes, paths=[*flow.paths, loop]))
    assert v.valid
    assert [(w.target, w.id) for w in v.warnings if "loops back to the same" in w.message] == [
        ("path", loop.id)
    ]


def test_single_path_step_is_a_warning() -> None:
    flow, _, step, _ = linear()
    v = validate_flow(flow)
    assert v.valid and issues(v, "warnings") == {("node", step.id, "paths")}
    flow2, n, _ = branching()
    assert ("node", n["qualify"].id, "paths") not in issues(validate_flow(flow2), "warnings")


def test_schema_rejects_structural_mistakes() -> None:
    start, end = node("start", "S"), node("end", "E")
    with pytest.raises(ValueError, match="unique"):
        FlowDefinition(nodes=[start, start], paths=[])
    with pytest.raises(ValueError, match="UUID"):
        FlowNode(id="n1", kind="step", name="X")
    with pytest.raises(ValueError, match="greeting"):
        FlowNode(id=str(uuid.uuid4()), kind="end", name="X", greeting="hi")
    with pytest.raises(ValueError, match="flow is required"):
        guided(mode="flow")
    with pytest.raises(ValueError, match="voicemail"):
        CallSettings(voicemail={"action": "leave_message", "message": " "})  # type: ignore[arg-type]
    assert CallSettings(key_terms=["Acme, Inc", "  ", "Acme Inc"]).key_terms == ["Acme Inc"]
    assert validate_flow(FlowDefinition(nodes=[start, end], paths=[path(start, end, "Go")])).valid


# ------------------------------------------------------------------ compiler


def test_guided_config_compiles_to_the_classic_two_node_shape() -> None:
    cfg = guided(closing="Say bye.", end_condition="They are done.")
    definition = build_definition(
        cfg, webhook=WebhookTarget(url="http://x/hook", credential_uuid="c1")
    )
    by_type = {n["type"]: n for n in definition["nodes"]}
    assert [n["type"] for n in definition["nodes"]] == [
        "trigger",
        "startCall",
        "endCall",
        "webhook",
    ]
    start, end = by_type["startCall"]["data"], by_type["endCall"]["data"]
    assert start["greeting"] == "Hi {{first_name}}" and start["greeting_type"] == "text"
    assert start["prompt"] == compile_prompt(cfg)
    assert start["allow_interrupt"] is True and start["allow_greeting_interrupt"] is True
    assert "extraction_enabled" not in start
    assert end["prompt"] == "Say bye." and end["add_global_prompt"] is False
    assert {v["name"] for v in end["extraction_variables"]} == {"budget", OPT_OUT_FIELD}
    [edge] = definition["edges"]
    assert (
        edge["source"] == by_type["startCall"]["id"] and edge["target"] == by_type["endCall"]["id"]
    )
    assert edge["data"] == {"label": "Wrap up", "condition": "They are done."}
    assert (
        by_type["webhook"]["data"]["payload_template"]["atlas_call_id"]
        == "{{initial_context.atlas_call_id}}"
    )
    assert GLOBAL_ID not in {n["id"] for n in definition["nodes"]}
    # Conversion is stable: the same guided config always yields the same ids.
    assert guided_to_flow(cfg) == guided_to_flow(cfg)
    assert validate_flow(guided_to_flow(cfg)).valid


def test_branching_flow_compiles_nodes_edges_and_global_rules() -> None:
    flow, n, p = branching()
    cfg = flow_config(flow, global_rules="Be brief.")
    compiled = compile_workflow(
        cfg, webhook=None, tool_uuids={"a1": "tool-uuid"}, document_uuids=None
    )
    nodes = {nd["id"]: nd for nd in compiled.definition["nodes"]}
    ids, pids = compiled.node_ids, compiled.path_ids

    assert set(ids.values()) == {f"n{i}" for i in range(1, 6)} and set(pids.values()) == {
        f"p{i}" for i in range(1, 7)
    }
    assert nodes[TRIGGER_ID]["type"] == "trigger" and REPORT_ID not in nodes
    assert nodes[GLOBAL_ID] == {
        "id": GLOBAL_ID,
        "type": "globalNode",
        "position": nodes[GLOBAL_ID]["position"],
        "data": {"name": "Global rules", "prompt": "Be brief."},
    }
    assert nodes[ids[n["start"].id]]["type"] == "startCall"
    assert nodes[ids[n["qualify"].id]]["type"] == "agentNode"
    for key in ("booked", "later"):
        data = nodes[ids[n[key].id]]["data"]
        assert nodes[ids[n[key].id]]["type"] == "endCall"
        assert data["extraction_enabled"] is True and data["add_global_prompt"] is False
        assert OPT_OUT_FIELD in {v["name"] for v in data["extraction_variables"]}
    assert {v["name"] for v in nodes[ids[n["booked"].id]]["data"]["extraction_variables"]} == {
        "slot",
        OPT_OUT_FIELD,
    }
    # Steps and Start extract their own captures (no opt-out: that is decided at the End).
    assert [v["name"] for v in nodes[ids[n["qualify"].id]]["data"]["extraction_variables"]] == [
        "budget"
    ]
    assert "extraction_enabled" not in nodes[ids[n["book"].id]]["data"]
    assert nodes[ids[n["qualify"].id]]["data"]["add_global_prompt"] is True

    edges = {e["id"]: e for e in compiled.definition["edges"]}
    fit = edges[pids[p["fit"].id]]
    assert fit["source"] == ids[n["qualify"].id] and fit["target"] == ids[n["book"].id]
    assert fit["data"] == {
        "label": "Good fit",
        "condition": "They qualify",
        "transition_speech": "One moment.",
        "transition_speech_type": "text",
    }
    assert edges[pids[p["go"].id]]["data"] == {"label": "Ready", "condition": "They can talk"}

    # Without rules there is no global node; without a resolver no tool refs are emitted.
    plain = compile_workflow(flow_config(flow), webhook=None)
    assert GLOBAL_ID not in {nd["id"] for nd in plain.definition["nodes"]}


def test_action_and_knowledge_references_resolve_to_runtime_uuids() -> None:
    start = node("start", "S", action_ids=["a1", "missing"], knowledge_ids=["k1"])
    end = node("end", "E", action_ids=["a1"])
    flow = FlowDefinition(nodes=[start, end], paths=[path(start, end, "Go")])
    compiled = compile_workflow(
        flow_config(flow), webhook=None, tool_uuids={"a1": "tool-1"}, document_uuids={"k1": "doc-1"}
    )
    nodes = {nd["type"]: nd["data"] for nd in compiled.definition["nodes"]}
    assert nodes["startCall"]["tool_uuids"] == ["tool-1"] and nodes["startCall"][
        "document_uuids"
    ] == ["doc-1"]
    assert "tool_uuids" not in nodes["endCall"], "Ends cannot use actions"


def test_configurations_map_call_settings() -> None:
    cfg = guided(
        call_settings=CallSettings(
            max_call_seconds=600,
            idle_timeout_seconds=15,
            turn_start="min_words",
            turn_start_min_words=3,
            turn_stop="turn_analyzer",
            smart_turn_stop_seconds=1.5,
            key_terms=["Acme", "Hina"],
            voicemail={"action": "leave_message", "message": "Sorry we missed you."},  # type: ignore[arg-type]
            ambient={"enabled": True, "volume": 0.2},  # type: ignore[arg-type]
        )
    )
    c = build_configurations(cfg)
    assert c["max_call_duration"] == 600 and c["max_user_idle_timeout"] == 15.0
    assert c["turn_start_strategy"] == "min_words" and c["turn_start_min_words"] == 3
    assert c["turn_stop_strategy"] == "turn_analyzer" and c["smart_turn_stop_secs"] == 1.5
    assert c["dictionary"] == "Acme, Hina"
    assert c["ambient_noise_configuration"] == {"enabled": True, "volume": 0.2}
    assert c["voicemail_detection"] == {
        "enabled": True,
        "use_workflow_llm": True,
        "voicemail_action": "leave_message",
        "voicemail_message": {"text": "Sorry we missed you."},
        "screening_message": {"text": ""},
        "screening_wait_ms": 30000,
    }
    assert next(d["code"] for d in c["call_dispositions"]) == "qualified"
    defaults = build_configurations(guided())
    assert defaults["max_call_duration"] == 300 and defaults["dictionary"] == ""
    assert defaults["voicemail_detection"]["voicemail_action"] == "hangup"


def test_runtime_errors_translate_back_to_atlas_ids() -> None:
    flow, n, p = branching()
    compiled = compile_workflow(flow_config(flow, global_rules="x"), webhook=None)
    rid = compiled.node_ids[n["qualify"].id]
    issue = compiled.translate(
        {"kind": "node", "id": rid, "field": "data.prompt", "message": "Prompt is required"}
    )
    assert (issue.target, issue.id, issue.field, issue.message) == (
        "node",
        n["qualify"].id,
        "prompt",
        "Prompt is required",
    )
    cap = compiled.translate(
        {"kind": "node", "id": rid, "field": "data.extraction_variables.0.name", "message": "m"}
    )
    assert cap.field == "capture_fields"
    edge = compiled.translate(
        {
            "kind": "edge",
            "id": compiled.path_ids[p["fit"].id],
            "field": "data.label",
            "message": "dup",
        }
    )
    assert (edge.target, edge.id, edge.field) == ("path", p["fit"].id, "name")
    when = compiled.translate(
        {
            "kind": "edge",
            "id": compiled.path_ids[p["go"].id],
            "field": "data.condition",
            "message": "m",
        }
    )
    assert when.field == "when"
    rules = compiled.translate(
        {"kind": "node", "id": GLOBAL_ID, "field": "data.prompt", "message": "m"}
    )
    assert (rules.target, rules.field) == ("flow", "global_rules")
    unknown = compiled.translate(
        {"kind": "workflow", "id": None, "field": None, "message": "Too many nodes"}
    )
    assert (unknown.target, unknown.id, unknown.field) == ("flow", "", None)


def test_prompt_preview_prepends_global_rules_except_on_ends() -> None:
    flow, n, _ = branching()
    cfg = flow_config(flow, global_rules=" Be brief. ")
    start = compose_prompt(cfg)
    assert start and start.node_id == n["start"].id and start.greeting == "Hello there"
    assert start.prompt == "Be brief.\n\nDo Open." and start.global_rules_applied
    end = compose_prompt(cfg, n["booked"].id)
    assert end and end.prompt == "Do Booked." and not end.global_rules_applied
    opted_out = flow_config(
        FlowDefinition(
            nodes=[n2 := node("start", "S", use_global_rules=False), e := node("end", "E")],
            paths=[path(n2, e, "Go")],
        ),
        global_rules="Rules",
    )
    assert (preview := compose_prompt(opted_out)) and preview.prompt == "Do S."
    assert compose_prompt(cfg, "nope") is None


def test_flow_template_is_valid_and_compiles() -> None:
    template = next(t for t in TEMPLATES if t.id == "qualify_and_book")
    assert template.config.mode == "flow" and template.config.flow is not None
    assert validate_flow(template.config.flow).valid
    definition = build_definition(template.config, webhook=None)
    assert [n["type"] for n in definition["nodes"]].count("endCall") == 2
    assert any(n["type"] == "globalNode" for n in definition["nodes"])
    assert "transfer" not in str(definition).lower()


# ------------------------------------------------------------------ endpoints


class RejectingRuntime(ToolRuntime, KnowledgeRuntime):
    """Rejects publishing when a step is named "Reject me", citing the runtime node id."""

    def _workflows(self, org: Any, m: str, path: str, body: Any) -> httpx.Response:
        if m == "POST" and path.endswith("/publish"):
            wf = next(iter(org.workflows.values()))
            for nd in wf["draft"]["nodes"]:
                if nd["type"] == "agentNode" and nd["data"]["name"] == "Reject me":
                    return _json(
                        422,
                        {
                            "detail": {
                                "is_valid": False,
                                "errors": [
                                    {
                                        "kind": "node",
                                        "id": nd["id"],
                                        "field": "data.prompt",
                                        "message": "Prompt is required",
                                    }
                                ],
                            }
                        },
                    )
        return super()._workflows(org, m, path, body)


@pytest.fixture
def runtime() -> Iterator[RejectingRuntime]:
    fake = RejectingRuntime()
    with respx.mock(assert_all_called=False) as router:
        router.route(host="runtime.test").mock(side_effect=fake.handle)
        yield fake


async def test_convert_to_flow_creates_an_equivalent_version_once(
    client: httpx.AsyncClient, owner: Account, runtime: RejectingRuntime
) -> None:
    agent = await _agent(client, owner, template="lead_qualifier")
    url = f"/api/v1/agents/{agent['id']}"
    r = await client.post(f"{url}/convert-to-flow", headers=owner.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["latest_version"] == 2 and body["config"]["mode"] == "flow"
    flow = body["config"]["flow"]
    kinds = sorted(n["kind"] for n in flow["nodes"])
    assert kinds == ["end", "start"] and len(flow["paths"]) == 1
    start = next(n for n in flow["nodes"] if n["kind"] == "start")
    assert start["greeting"] == agent["config"]["greeting"]  # type: ignore[index]
    assert "Acme Realty" in start["prompt"]
    assert flow["paths"][0]["name"] == "Wrap up"
    # Guided fields are kept (lossless) but no longer drive the published graph.
    assert body["config"]["instructions"] == agent["config"]["instructions"]  # type: ignore[index]
    again = await client.post(f"{url}/convert-to-flow", headers=owner.headers)
    assert again.status_code == 409
    versions = (await client.get(f"{url}/versions", headers=owner.headers)).json()
    assert versions[0]["note"] == "Switched to the flow builder"


async def test_validate_endpoint_reports_errors_and_warnings(
    client: httpx.AsyncClient, owner: Account, runtime: RejectingRuntime
) -> None:
    agent = await _agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}"
    ok = await client.post(f"{url}/validate", headers=owner.headers)
    assert ok.status_code == 200 and ok.json() == {"valid": True, "errors": [], "warnings": []}

    start, step, end = node("start", "Open"), node("step", "Dead end"), node("end", "Done")
    cfg = {
        **agent["config"],
        "mode": "flow",
        "flow": FlowDefinition(  # type: ignore[dict-item]
            nodes=[start, step, end], paths=[path(start, end, "Finish", when="")]
        ).model_dump(mode="json"),
    }
    saved = await client.put(f"{url}/config", headers=owner.headers, json={"config": cfg})
    assert saved.status_code == 200, saved.text
    v = (await client.post(f"{url}/validate", headers=owner.headers)).json()
    assert v["valid"] is False
    found = {(e["target"], e["id"], e["field"]) for e in v["errors"]}
    assert found == {
        ("node", step.id, "paths"),
        ("node", step.id, None),
        ("path", cfg["flow"]["paths"][0]["id"], "when"),
    }  # type: ignore[index]

    viewer_ok = await client.post(f"{url}/validate", headers=owner.headers)
    assert viewer_ok.status_code == 200


async def test_compiled_prompt_endpoint(
    client: httpx.AsyncClient, owner: Account, runtime: RejectingRuntime
) -> None:
    agent = await _agent(client, owner, template="qualify_and_book")
    url = f"/api/v1/agents/{agent['id']}"
    r = await client.get(f"{url}/compiled-prompt", headers=owner.headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "start" and body["global_rules_applied"] is True
    assert body["prompt"].startswith("You are Sarah, calling on behalf of Acme Realty.")
    assert "Acme Realty" in body["greeting"]
    flow = agent["config"]["flow"]  # type: ignore[index]
    end = next(n for n in flow["nodes"] if n["kind"] == "end")
    r2 = await client.get(
        f"{url}/compiled-prompt", headers=owner.headers, params={"node_id": end["id"]}
    )
    assert r2.json()["global_rules_applied"] is False and r2.json()["prompt"] == end["prompt"]
    missing = await client.get(
        f"{url}/compiled-prompt", headers=owner.headers, params={"node_id": "nope"}
    )
    assert missing.status_code == 404


async def test_publish_flow_agent_validates_locally_then_with_the_runtime(
    client: httpx.AsyncClient, owner: Account, runtime: RejectingRuntime
) -> None:
    await _setup_models(client, owner)
    agent = await _agent(client, owner, template="qualify_and_book")
    url = f"/api/v1/agents/{agent['id']}"

    r = await client.post(f"{url}/publish", headers=owner.headers, json={})
    assert r.status_code == 200, r.text
    sent = runtime.last("PUT", r"^/workflow/\d+$")
    types = [n["type"] for n in sent["workflow_definition"]["nodes"]]
    assert types.count("endCall") == 2 and "globalNode" in types and "webhook" in types
    assert sent["workflow_configurations"]["voicemail_detection"]["voicemail_action"] == "hangup"
    assert sent["workflow_configurations"]["max_call_duration"] == 300

    # A broken flow never reaches the runtime; the response names the node and field.
    cfg: dict[str, Any] = dict(agent["config"])  # type: ignore[arg-type]
    broken = dict(cfg["flow"])
    broken["nodes"] = [{**n, "prompt": ""} if n["kind"] == "step" else n for n in broken["nodes"]]
    cfg["flow"] = broken
    await client.put(f"{url}/config", headers=owner.headers, json={"config": cfg})
    publishes = runtime.count("POST", r"^/workflow/\d+/publish$")
    r = await client.post(f"{url}/publish", headers=owner.headers, json={})
    assert r.status_code == 422, r.text
    step = next(n for n in broken["nodes"] if n["kind"] == "step")
    assert r.json()["error"]["details"] == [
        {
            "target": "node",
            "id": step["id"],
            "field": "prompt",
            "message": "Describe what the agent does at this step",
            "section": "Step: Qualify",
        }
    ]
    assert runtime.count("POST", r"^/workflow/\d+/publish$") == publishes

    # A runtime rejection is translated to the ATLAS node id and recorded on the agent.
    rejected = dict(cfg)
    rejected["flow"] = {
        **broken,
        "nodes": [
            {**n, "prompt": "Ask things.", "name": "Reject me"} if n["kind"] == "step" else n
            for n in broken["nodes"]
        ],
    }
    await client.put(f"{url}/config", headers=owner.headers, json={"config": rejected})
    r = await client.post(f"{url}/publish", headers=owner.headers, json={})
    assert r.status_code == 422, r.text
    [detail] = r.json()["error"]["details"]
    assert detail["target"] == "node" and detail["id"] == step["id"] and detail["field"] == "prompt"
    assert detail["section"] == "Step: Reject me" and detail["message"] == "Prompt is required"
    detail_agent = (await client.get(url, headers=owner.headers)).json()
    assert detail_agent["sync_status"] == "error" and detail_agent["published_version"] == 1


async def test_attached_actions_are_checked_and_sent_as_tool_references(
    client: httpx.AsyncClient, owner: Account, runtime: RejectingRuntime
) -> None:
    await _setup_models(client, owner)
    action = (await create_action(client, owner)).json()
    assert action["function_name"] == "book_a_viewing"
    agent = await _agent(client, owner)
    url = f"/api/v1/agents/{agent['id']}"

    start = node("start", "Open", action_ids=[action["id"]])
    gone = node("step", "Detail", action_ids=[str(uuid.uuid4())], knowledge_ids=[str(uuid.uuid4())])
    end = node("end", "Done")
    clash = path(start, gone, "Book a viewing")
    flow = FlowDefinition(nodes=[start, gone, end], paths=[clash, path(gone, end, "Finish")])
    cfg: dict[str, Any] = {**agent["config"], "mode": "flow", "flow": flow.model_dump(mode="json")}  # type: ignore[dict-item]
    assert (
        await client.put(f"{url}/config", headers=owner.headers, json={"config": cfg})
    ).status_code == 200

    v = (await client.post(f"{url}/validate", headers=owner.headers)).json()
    found = {(e["target"], e["id"], e["field"]) for e in v["errors"]}
    assert found == {
        ("path", clash.id, "name"),
        ("node", gone.id, "action_ids"),
        ("node", gone.id, "knowledge_ids"),
    }
    assert any("book_a_viewing" in e["message"] for e in v["errors"])

    # Fix the clash and the dangling references; the action travels as a tool reference.
    fixed = flow.model_copy(
        update={
            "nodes": [start, gone.model_copy(update={"action_ids": [], "knowledge_ids": []}), end],
            "paths": [clash.model_copy(update={"name": "Viewing booked"}), flow.paths[1]],
        }
    )
    cfg["flow"] = fixed.model_dump(mode="json")
    await client.put(f"{url}/config", headers=owner.headers, json={"config": cfg})
    r = await client.post(f"{url}/publish", headers=owner.headers, json={})
    assert r.status_code == 200, r.text
    sent = runtime.last("PUT", r"^/workflow/\d+$")
    start_node = next(n for n in sent["workflow_definition"]["nodes"] if n["type"] == "startCall")
    assert start_node["data"]["tool_uuids"] == [action["id"]]
    assert "transfer" not in str(sent).lower()
