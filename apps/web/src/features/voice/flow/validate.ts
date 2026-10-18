/**
 * Instant validation, mirroring the API's `validate_flow` rule for rule so the canvas
 * can mark problems as you edit. The server remains authoritative: it also checks the
 * attached actions and documents still exist.
 */
import type { FlowDefinition, FlowIssue, FlowNode, FlowPath, FlowValidation, IssueTarget } from "@/features/voice/flow/types";

/** The function name the model calls to take a path: the API derives it the same way. */
export function pathFunctionName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]/g, "_");
}

const issue = (target: IssueTarget, id: string, field: string | null, message: string): FlowIssue => ({ target, id, field, message });

function blankPromptMessage(node: FlowNode): string {
  if (node.kind === "start") return "Describe what the agent does once the call starts";
  if (node.kind === "end") return "Describe how the agent ends the call";
  return "Describe what the agent does at this step";
}

function reachable(startId: string, nodes: Map<string, FlowNode>, outgoing: Map<string, FlowPath[]>): Set<string> {
  const seen = new Set([startId]);
  const stack = [startId];
  while (stack.length) {
    const current = stack.pop() as string;
    for (const p of outgoing.get(current) ?? []) {
      if (nodes.has(p.to_node) && !seen.has(p.to_node)) {
        seen.add(p.to_node);
        stack.push(p.to_node);
      }
    }
  }
  return seen;
}

/** Paths that close a cycle, found by DFS colouring from Start. */
function backEdges(startId: string, outgoing: Map<string, FlowPath[]>): FlowPath[] {
  const colour = new Map<string, 1 | 2>();
  const back: FlowPath[] = [];
  const visit = (id: string) => {
    colour.set(id, 1);
    for (const p of outgoing.get(id) ?? []) {
      const c = colour.get(p.to_node);
      if (c === 1) back.push(p);
      else if (c === undefined) visit(p.to_node);
    }
    colour.set(id, 2);
  };
  visit(startId);
  return back;
}

/**
 * @param actionNames action id → function name, for the actions attached to nodes;
 *   a path cannot share a function name with an action on its source node.
 */
export function validateFlow(flow: FlowDefinition, actionNames: Record<string, string> = {}): FlowValidation {
  const errors: FlowIssue[] = [];
  const warnings: FlowIssue[] = [];
  const nodes = new Map(flow.nodes.map((n) => [n.id, n]));
  const outgoing = new Map<string, FlowPath[]>();

  const starts = flow.nodes.filter((n) => n.kind === "start");
  const ends = flow.nodes.filter((n) => n.kind === "end");
  if (starts.length === 0) errors.push(issue("flow", "", null, "Add a Start: every flow begins with one"));
  for (const extra of starts.slice(1)) errors.push(issue("node", extra.id, null, "Only one Start is allowed"));
  if (ends.length === 0) errors.push(issue("flow", "", null, "Add at least one End so calls can finish"));

  for (const p of flow.paths) {
    const src = nodes.get(p.from_node);
    const dst = nodes.get(p.to_node);
    if (!src) errors.push(issue("path", p.id, "from_node", "This path starts at a missing node"));
    if (!dst) errors.push(issue("path", p.id, "to_node", "This path leads to a missing node"));
    if (!src || !dst) continue;
    outgoing.set(src.id, [...(outgoing.get(src.id) ?? []), p]);
    if (dst.kind === "start") errors.push(issue("path", p.id, "to_node", "Paths cannot lead back to Start"));
    if (src.kind === "end") errors.push(issue("path", p.id, "from_node", "An End has no paths out of it"));
    if (!p.when.trim()) errors.push(issue("path", p.id, "when", "Describe when the agent should take this path"));
  }

  const seenNames = new Map<string, string>();
  for (const n of flow.nodes) {
    if (!n.prompt.trim()) errors.push(issue("node", n.id, "prompt", blankPromptMessage(n)));
    if (n.kind === "start" && !(n.greeting ?? "").trim()) errors.push(issue("node", n.id, "greeting", "Write the first thing the agent says"));
    const key = n.name.trim().toLowerCase();
    if (seenNames.has(key)) errors.push(issue("node", n.id, "name", "Another node already has this name"));
    else seenNames.set(key, n.id);
    if (n.kind !== "end" && !(outgoing.get(n.id) ?? []).length) errors.push(issue("node", n.id, "paths", "Add a path out of this step, or make it an End"));
  }

  for (const [srcId, paths] of outgoing) {
    const src = nodes.get(srcId) as FlowNode;
    const taken = new Set(src.action_ids.map((a) => actionNames[a]).filter((x): x is string => Boolean(x)));
    const byFunction = new Map<string, FlowPath[]>();
    for (const p of paths) {
      const fn = pathFunctionName(p.name);
      byFunction.set(fn, [...(byFunction.get(fn) ?? []), p]);
    }
    for (const [fn, group] of byFunction) {
      if (group.length > 1) for (const p of group) errors.push(issue("path", p.id, "name", `Another path from "${src.name}" has the same name`));
      if (taken.has(fn)) for (const p of group) errors.push(issue("path", p.id, "name", `Clashes with the action "${fn}" on "${src.name}"`));
    }
  }

  const start = starts[0];
  if (start) {
    const reached = reachable(start.id, nodes, outgoing);
    for (const n of flow.nodes) if (!reached.has(n.id)) errors.push(issue("node", n.id, null, "Unreachable: no path leads here from Start"));
    for (const p of backEdges(start.id, outgoing)) {
      warnings.push(issue("path", p.id, null, p.from_node === p.to_node ? "This path loops back to the same step" : "This path loops back to an earlier step"));
    }
  }

  for (const n of flow.nodes) {
    if (n.kind !== "step") continue;
    const targets = new Set((outgoing.get(n.id) ?? []).map((p) => p.to_node));
    if (targets.size === 1) warnings.push(issue("node", n.id, "paths", "Single path: every path leads to the same place. Consider merging this step into the next one."));
  }

  return { valid: errors.length === 0, errors, warnings };
}

/** Issues grouped by node/path id, for markers on the canvas. */
export function issuesByTarget(issues: FlowIssue[]): Map<string, FlowIssue[]> {
  const map = new Map<string, FlowIssue[]>();
  for (const i of issues) {
    const key = `${i.target}:${i.id}`;
    map.set(key, [...(map.get(key) ?? []), i]);
  }
  return map;
}

export const targetKey = (target: IssueTarget, id: string) => `${target}:${id}`;
