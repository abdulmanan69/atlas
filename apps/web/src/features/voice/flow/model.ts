/**
 * Editor state for the flow canvas: the flow, the selection, and a bounded undo history.
 * A pure reducer so every edit (toolbar, canvas, inspector, keyboard) goes through one
 * place and is undoable. Rapid edits to the same field coalesce into one history entry
 * so undo steps back a thought, not a keystroke.
 */
import { LIMITS, uuid, type FlowDefinition, type FlowNode, type FlowPath, type NodeKind, type XY } from "@/features/voice/flow/types";

export type Selection = { kind: "node" | "path"; id: string } | null;

export interface EditorState {
  flow: FlowDefinition;
  selection: Selection;
  past: FlowDefinition[];
  future: FlowDefinition[];
  lastEdit: { key: string; at: number } | null;
}

export type EditorAction =
  | { type: "add_node"; kind: Exclude<NodeKind, "start">; position?: XY }
  | { type: "connect"; from: string; to: string }
  | { type: "update_node"; id: string; patch: Partial<Omit<FlowNode, "id" | "kind">>; coalesce?: string }
  | { type: "update_path"; id: string; patch: Partial<Omit<FlowPath, "id" | "from_node" | "to_node">>; coalesce?: string }
  | { type: "move"; positions: Record<string, XY> }
  | { type: "delete"; selection?: Selection }
  | { type: "select"; selection: Selection }
  | { type: "replace"; flow: FlowDefinition; select?: Selection }
  | { type: "undo" }
  | { type: "redo" };

export const HISTORY_LIMIT = 50;
export const COALESCE_MS = 1000;

export function createState(flow: FlowDefinition): EditorState {
  return { flow, selection: null, past: [], future: [], lastEdit: null };
}

const STEP_SIZE = { w: 280, h: 120 };

/** A free spot to the right of the right-most node, so new nodes never stack. */
function nextPosition(nodes: FlowNode[]): XY {
  if (!nodes.length) return { x: 0, y: 0 };
  const maxX = Math.max(...nodes.map((n) => n.position.x));
  const rightmost = nodes.filter((n) => n.position.x === maxX);
  const below = Math.max(...rightmost.map((n) => n.position.y));
  return rightmost.length >= 3 ? { x: maxX + STEP_SIZE.w + 120, y: 0 } : { x: maxX, y: below + STEP_SIZE.h + 80 };
}

function uniqueName(base: string, nodes: FlowNode[]): string {
  const taken = new Set(nodes.map((n) => n.name.trim().toLowerCase()));
  if (!taken.has(base.toLowerCase())) return base;
  for (let i = 2; ; i++) {
    const candidate = `${base} ${i}`;
    if (!taken.has(candidate.toLowerCase())) return candidate;
  }
}

export function newNode(kind: Exclude<NodeKind, "start">, nodes: FlowNode[], position?: XY): FlowNode {
  return {
    id: uuid(),
    kind,
    name: uniqueName(kind === "end" ? "End" : "Step", nodes),
    position: position ?? nextPosition(nodes),
    prompt: "",
    greeting: null,
    allow_interrupt: true,
    use_global_rules: true,
    capture_fields: [],
    action_ids: [],
    knowledge_ids: [],
  };
}

export function newPath(from: string, to: string, paths: FlowPath[]): FlowPath {
  const outgoing = paths.filter((p) => p.from_node === from).length;
  return { id: uuid(), from_node: from, to_node: to, name: `Path ${outgoing + 1}`, when: "", say_while_moving: null };
}

/** Whether a connection may be drawn: never into Start, never out of an End. */
export function canConnect(flow: FlowDefinition, from: string, to: string): boolean {
  const src = flow.nodes.find((n) => n.id === from);
  const dst = flow.nodes.find((n) => n.id === to);
  if (!src || !dst) return false;
  if (dst.kind === "start" || src.kind === "end") return false;
  return flow.paths.length < LIMITS.paths;
}

function commit(state: EditorState, flow: FlowDefinition, selection: Selection, lastEdit: EditorState["lastEdit"] = null): EditorState {
  const past = [...state.past, state.flow].slice(-HISTORY_LIMIT);
  return { flow, selection, past, future: [], lastEdit };
}

function coalesced(state: EditorState, flow: FlowDefinition, key: string | undefined, now: number): EditorState {
  if (key && state.lastEdit && state.lastEdit.key === key && now - state.lastEdit.at < COALESCE_MS) {
    return { ...state, flow, lastEdit: { key, at: now } };
  }
  return commit(state, flow, state.selection, key ? { key, at: now } : null);
}

export function reduce(state: EditorState, action: EditorAction, now: number = Date.now()): EditorState {
  const { flow } = state;
  switch (action.type) {
    case "add_node": {
      if (flow.nodes.length >= LIMITS.nodes) return state;
      const node = newNode(action.kind, flow.nodes, action.position);
      return commit(state, { ...flow, nodes: [...flow.nodes, node] }, { kind: "node", id: node.id });
    }
    case "connect": {
      if (!canConnect(flow, action.from, action.to)) return state;
      const path = newPath(action.from, action.to, flow.paths);
      return commit(state, { ...flow, paths: [...flow.paths, path] }, { kind: "path", id: path.id });
    }
    case "update_node": {
      const node = flow.nodes.find((n) => n.id === action.id);
      if (!node) return state;
      const next = { ...node, ...action.patch, id: node.id, kind: node.kind };
      const nodes = flow.nodes.map((n) => (n.id === node.id ? next : n));
      return coalesced(state, { ...flow, nodes }, action.coalesce && `node:${node.id}:${action.coalesce}`, now);
    }
    case "update_path": {
      const path = flow.paths.find((p) => p.id === action.id);
      if (!path) return state;
      const next = { ...path, ...action.patch, id: path.id, from_node: path.from_node, to_node: path.to_node };
      const paths = flow.paths.map((p) => (p.id === path.id ? next : p));
      return coalesced(state, { ...flow, paths }, action.coalesce && `path:${path.id}:${action.coalesce}`, now);
    }
    case "move": {
      const nodes = flow.nodes.map((n) => {
        const to = action.positions[n.id];
        return to && (to.x !== n.position.x || to.y !== n.position.y) ? { ...n, position: { x: to.x, y: to.y } } : n;
      });
      if (nodes.every((n, i) => n === flow.nodes[i])) return state;
      return commit(state, { ...flow, nodes }, state.selection);
    }
    case "delete": {
      const sel = action.selection === undefined ? state.selection : action.selection;
      if (!sel) return state;
      if (sel.kind === "path") {
        if (!flow.paths.some((p) => p.id === sel.id)) return state;
        return commit(state, { ...flow, paths: flow.paths.filter((p) => p.id !== sel.id) }, null);
      }
      const node = flow.nodes.find((n) => n.id === sel.id);
      if (!node || node.kind === "start") return state;
      return commit(
        state,
        { ...flow, nodes: flow.nodes.filter((n) => n.id !== sel.id), paths: flow.paths.filter((p) => p.from_node !== sel.id && p.to_node !== sel.id) },
        null,
      );
    }
    case "select": {
      const sel = action.selection;
      const exists = !sel || (sel.kind === "node" ? flow.nodes.some((n) => n.id === sel.id) : flow.paths.some((p) => p.id === sel.id));
      const next = exists ? sel : null;
      if (next?.id === state.selection?.id && next?.kind === state.selection?.kind) return state;
      return { ...state, selection: next, lastEdit: null };
    }
    case "replace":
      return commit(state, action.flow, action.select === undefined ? state.selection : action.select);
    case "undo": {
      const previous = state.past[state.past.length - 1];
      if (!previous) return state;
      return { flow: previous, selection: keepSelection(previous, state.selection), past: state.past.slice(0, -1), future: [state.flow, ...state.future], lastEdit: null };
    }
    case "redo": {
      const [next, ...rest] = state.future;
      if (!next) return state;
      return { flow: next, selection: keepSelection(next, state.selection), past: [...state.past, state.flow].slice(-HISTORY_LIMIT), future: rest, lastEdit: null };
    }
  }
}

function keepSelection(flow: FlowDefinition, selection: Selection): Selection {
  if (!selection) return null;
  const exists = selection.kind === "node" ? flow.nodes.some((n) => n.id === selection.id) : flow.paths.some((p) => p.id === selection.id);
  return exists ? selection : null;
}
