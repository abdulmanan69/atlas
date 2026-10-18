"""Flow validation and prompt composition (ADR 0005). Pure functions, no I/O.

ATLAS validates more strictly than the runtime: unreachable nodes, dead ends and blank
text are errors; cycles and single-path steps are warnings. Every issue names the node
or path it belongs to and the field to fix, so the editor can jump straight to it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping

from app.integrations.voice_runtime.workflow_builder import effective_flow
from app.schemas.voice import (
    AgentConfig,
    CompiledPrompt,
    FlowDefinition,
    FlowIssue,
    FlowNode,
    FlowPath,
    FlowValidation,
    IssueTarget,
    path_function_name,
)


def _issue(target: IssueTarget, id_: str, field: str | None, message: str) -> FlowIssue:
    return FlowIssue(target=target, id=id_, field=field, message=message)


def validate_flow(
    flow: FlowDefinition, action_function_names: Mapping[str, str] | None = None
) -> FlowValidation:
    """Check a flow against the rules a publishable conversation must satisfy.

    `action_function_names` maps an action id to the function name the model calls for
    it, so a path cannot shadow an action attached to the same node (the runtime rejects
    that collision too; checking here gives the author the ATLAS ids).
    """
    errors: list[FlowIssue] = []
    warnings: list[FlowIssue] = []
    names_of = action_function_names or {}
    nodes = {n.id: n for n in flow.nodes}
    outgoing: dict[str, list[str]] = defaultdict(list)  # node id → path ids
    incoming: dict[str, list[str]] = defaultdict(list)
    paths = {p.id: p for p in flow.paths}

    # --- structure: exactly one Start, at least one End
    starts = [n for n in flow.nodes if n.kind == "start"]
    ends = [n for n in flow.nodes if n.kind == "end"]
    if not starts:
        errors.append(_issue("flow", "", None, "Add a Start: every flow begins with one"))
    for extra in starts[1:]:
        errors.append(_issue("node", extra.id, None, "Only one Start is allowed"))
    if not ends:
        errors.append(_issue("flow", "", None, "Add at least one End so calls can finish"))

    # --- paths: endpoints exist, Start has no incoming, End has no outgoing
    for p in flow.paths:
        src, dst = nodes.get(p.from_node), nodes.get(p.to_node)
        if src is None:
            errors.append(_issue("path", p.id, "from_node", "This path starts at a missing node"))
        if dst is None:
            errors.append(_issue("path", p.id, "to_node", "This path leads to a missing node"))
        if src is None or dst is None:
            continue
        outgoing[src.id].append(p.id)
        incoming[dst.id].append(p.id)
        if dst.kind == "start":
            errors.append(_issue("path", p.id, "to_node", "Paths cannot lead back to Start"))
        if src.kind == "end":
            errors.append(_issue("path", p.id, "from_node", "An End has no paths out of it"))
        if not p.when.strip():
            errors.append(
                _issue("path", p.id, "when", "Describe when the agent should take this path")
            )

    # --- nodes: text, names, outgoing paths
    seen_names: dict[str, str] = {}
    for n in flow.nodes:
        if not n.prompt.strip():
            errors.append(_issue("node", n.id, "prompt", _blank_prompt_message(n)))
        if n.kind == "start" and not (n.greeting or "").strip():
            errors.append(_issue("node", n.id, "greeting", "Write the first thing the agent says"))
        key = n.name.casefold()
        if key in seen_names:
            errors.append(_issue("node", n.id, "name", "Another node already has this name"))
        else:
            seen_names[key] = n.id
        if n.kind != "end" and not outgoing.get(n.id):
            errors.append(
                _issue(
                    "node",
                    n.id,
                    "paths",
                    "Add a path out of this step, or make it an End",
                )
            )

    # --- path names: unique per source after normalisation, no collision with actions
    for src_id, path_ids in outgoing.items():
        src = nodes[src_id]
        taken = {names_of[a] for a in src.action_ids if a in names_of}
        by_function: dict[str, list[str]] = defaultdict(list)
        for pid in path_ids:
            by_function[path_function_name(paths[pid].name)].append(pid)
        for function, pids in by_function.items():
            if len(pids) > 1:
                for pid in pids:
                    errors.append(
                        _issue(
                            "path",
                            pid,
                            "name",
                            f'Another path from "{src.name}" has the same name',
                        )
                    )
            if function in taken:
                for pid in pids:
                    errors.append(
                        _issue(
                            "path",
                            pid,
                            "name",
                            f'Clashes with the action "{function}" on "{src.name}"',
                        )
                    )

    # --- reachability from Start, cycles
    if starts:
        start = starts[0]
        reached = _reachable(start.id, nodes, outgoing, paths)
        for n in flow.nodes:
            if n.id not in reached:
                errors.append(
                    _issue("node", n.id, None, "Unreachable: no path leads here from Start")
                )
        for pid in _back_edges(start.id, outgoing, paths):
            p = paths[pid]
            message = (
                "This path loops back to the same step"
                if p.from_node == p.to_node
                else "This path loops back to an earlier step"
            )
            warnings.append(_issue("path", pid, None, message))

    # --- single-path steps
    for n in flow.nodes:
        if n.kind != "step":
            continue
        targets = {paths[pid].to_node for pid in outgoing.get(n.id, [])}
        if len(targets) == 1:
            warnings.append(
                _issue(
                    "node",
                    n.id,
                    "paths",
                    "Single path: every path leads to the same place. Consider merging "
                    "this step into the next one.",
                )
            )

    return FlowValidation(valid=not errors, errors=errors, warnings=warnings)


def _blank_prompt_message(node: FlowNode) -> str:
    if node.kind == "start":
        return "Describe what the agent does once the call starts"
    if node.kind == "end":
        return "Describe how the agent ends the call"
    return "Describe what the agent does at this step"


def _reachable(
    start_id: str,
    nodes: Mapping[str, FlowNode],
    outgoing: Mapping[str, list[str]],
    paths: Mapping[str, FlowPath],
) -> set[str]:
    seen = {start_id}
    stack = [start_id]
    while stack:
        current = stack.pop()
        for pid in outgoing.get(current, []):
            target = paths[pid].to_node
            if target in nodes and target not in seen:
                seen.add(target)
                stack.append(target)
    return seen


def _back_edges(
    start_id: str, outgoing: Mapping[str, list[str]], paths: Mapping[str, FlowPath]
) -> list[str]:
    """Path ids that close a cycle, found by DFS colouring from Start."""
    white, grey, black = 0, 1, 2
    colour: dict[str, int] = defaultdict(int)
    back: list[str] = []

    def visit(node_id: str) -> None:
        colour[node_id] = grey
        for pid in outgoing.get(node_id, []):
            target = paths[pid].to_node
            if colour[target] == grey:
                back.append(pid)
            elif colour[target] == white:
                visit(target)
        colour[node_id] = black

    visit(start_id)
    # Nodes unreachable from Start are reported separately; their cycles are irrelevant.
    return back


# ------------------------------------------------------------------ prompt preview


def compose_prompt(cfg: AgentConfig, node_id: str | None = None) -> CompiledPrompt | None:
    """The system prompt the model receives at a node, composed like the runtime does.

    Global rules are prepended (separated by a blank line) to Start and Steps that opt
    in; Ends never get them. Template variables stay as written: they are filled per
    call. Returns None when the node does not exist.
    """
    flow = effective_flow(cfg)
    node = (
        next((n for n in flow.nodes if n.id == node_id), None)
        if node_id
        else next((n for n in flow.nodes if n.kind == "start"), None)
    )
    if node is None:
        return None
    rules = (cfg.global_rules or "").strip()
    applied = bool(rules) and node.use_global_rules and node.kind != "end"
    text = "\n\n".join(p for p in ((rules if applied else ""), node.prompt.strip()) if p)
    return CompiledPrompt(
        node_id=node.id,
        node_name=node.name,
        kind=node.kind,
        greeting=node.greeting if node.kind == "start" else None,
        prompt=text,
        global_rules_applied=applied,
    )
