/**
 * Flow canvas model. Mirrors the API's FlowDefinition with every field present so the
 * editor never has to reason about optional keys; `normalizeFlow` turns API data into
 * this shape and `fromForm` sends it back unchanged.
 */
import type { Schemas } from "@/lib/api";

export type NodeKind = "start" | "step" | "end";
export type CaptureType = "string" | "number" | "boolean";

export interface CaptureField {
  name: string;
  type: CaptureType;
  description: string;
}

export interface XY {
  x: number;
  y: number;
}

export interface FlowNode {
  id: string;
  kind: NodeKind;
  name: string;
  position: XY;
  prompt: string;
  greeting: string | null;
  allow_interrupt: boolean;
  use_global_rules: boolean;
  capture_fields: CaptureField[];
  action_ids: string[];
  knowledge_ids: string[];
}

export interface FlowPath {
  id: string;
  from_node: string;
  to_node: string;
  name: string;
  when: string;
  say_while_moving: string | null;
}

export interface Viewport extends XY {
  zoom: number;
}

export interface FlowDefinition {
  nodes: FlowNode[];
  paths: FlowPath[];
  viewport: Viewport | null;
}

export type IssueTarget = "node" | "path" | "flow";

export interface FlowIssue {
  target: IssueTarget;
  /** Node or path id; empty for flow-level issues. */
  id: string;
  field: string | null;
  message: string;
}

export interface FlowValidation {
  valid: boolean;
  errors: FlowIssue[];
  warnings: FlowIssue[];
}

export const KIND_LABEL: Record<NodeKind, string> = { start: "Start", step: "Step", end: "End" };

export const LIMITS = {
  name: 60,
  prompt: 20_000,
  greeting: 600,
  pathName: 40,
  when: 300,
  sayWhileMoving: 300,
  globalRules: 4000,
  nodes: 60,
  paths: 200,
  captures: 20,
} as const;

type ApiFlow = Schemas["FlowDefinition"];
type ApiNode = Schemas["FlowNode"];
type ApiPath = Schemas["FlowPath"];

export function normalizeNode(n: ApiNode): FlowNode {
  return {
    id: n.id,
    kind: n.kind,
    name: n.name,
    position: { x: n.position?.x ?? 0, y: n.position?.y ?? 0 },
    prompt: n.prompt ?? "",
    greeting: n.kind === "start" ? (n.greeting ?? "") : null,
    allow_interrupt: n.allow_interrupt ?? true,
    use_global_rules: n.use_global_rules ?? true,
    capture_fields: (n.capture_fields ?? []).map((f) => ({ name: f.name, type: f.type ?? "string", description: f.description })),
    action_ids: [...(n.action_ids ?? [])],
    knowledge_ids: [...(n.knowledge_ids ?? [])],
  };
}

export function normalizePath(p: ApiPath): FlowPath {
  return { id: p.id, from_node: p.from_node, to_node: p.to_node, name: p.name, when: p.when ?? "", say_while_moving: p.say_while_moving ?? null };
}

export function normalizeFlow(flow: ApiFlow | null | undefined): FlowDefinition {
  if (!flow) return { nodes: [], paths: [], viewport: null };
  const v = flow.viewport;
  return {
    nodes: (flow.nodes ?? []).map(normalizeNode),
    paths: (flow.paths ?? []).map(normalizePath),
    viewport: v ? { x: v.x ?? 0, y: v.y ?? 0, zoom: v.zoom ?? 1 } : null,
  };
}

/** A UUID for new nodes and paths; the API requires UUID ids. */
export function uuid(): string {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40;
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
