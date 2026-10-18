import { tidyUp } from "@/features/voice/flow/layout";
import { canConnect, COALESCE_MS, createState, HISTORY_LIMIT, reduce, type EditorState } from "@/features/voice/flow/model";
import { uuid, type FlowDefinition, type FlowNode } from "@/features/voice/flow/types";

function start(): FlowNode {
  return { id: uuid(), kind: "start", name: "Open", position: { x: 0, y: 0 }, prompt: "Hi.", greeting: "Hello", allow_interrupt: true, use_global_rules: true, capture_fields: [], action_ids: [], knowledge_ids: [] };
}

const base = (): FlowDefinition => ({ nodes: [start()], paths: [], viewport: null });

describe("flow reducer", () => {
  it("adds steps and ends with unique names and selects them", () => {
    let s = reduce(createState(base()), { type: "add_node", kind: "step" });
    s = reduce(s, { type: "add_node", kind: "step" });
    s = reduce(s, { type: "add_node", kind: "end" });
    expect(s.flow.nodes.map((n) => n.name)).toEqual(["Open", "Step", "Step 2", "End"]);
    expect(s.selection).toEqual({ kind: "node", id: s.flow.nodes[3]?.id });
    expect(s.flow.nodes[1]?.id).toMatch(/^[0-9a-f-]{36}$/);
    expect(s.past).toHaveLength(3);
  });

  it("connects nodes with a pre-named path and refuses illegal connections", () => {
    let s = reduce(createState(base()), { type: "add_node", kind: "step" });
    s = reduce(s, { type: "add_node", kind: "end" });
    const [open, step, end] = s.flow.nodes as [FlowNode, FlowNode, FlowNode];
    s = reduce(s, { type: "connect", from: open.id, to: step.id });
    s = reduce(s, { type: "connect", from: open.id, to: end.id });
    expect(s.flow.paths.map((p) => p.name)).toEqual(["Path 1", "Path 2"]);
    expect(s.selection).toEqual({ kind: "path", id: s.flow.paths[1]?.id });
    expect(canConnect(s.flow, step.id, open.id)).toBe(false);
    expect(canConnect(s.flow, end.id, step.id)).toBe(false);
    expect(reduce(s, { type: "connect", from: end.id, to: step.id })).toBe(s);
  });

  it("deletes the selection along with its paths, but never the Start", () => {
    let s = reduce(createState(base()), { type: "add_node", kind: "step" });
    const open = s.flow.nodes[0] as FlowNode;
    const step = s.flow.nodes[1] as FlowNode;
    s = reduce(s, { type: "connect", from: open.id, to: step.id });
    s = reduce(s, { type: "select", selection: { kind: "node", id: step.id } });
    s = reduce(s, { type: "delete" });
    expect(s.flow.nodes).toHaveLength(1);
    expect(s.flow.paths).toHaveLength(0);
    expect(s.selection).toBeNull();
    const untouched = reduce(s, { type: "delete", selection: { kind: "node", id: open.id } });
    expect(untouched).toBe(s);
  });

  it("undoes and redoes, keeping a bounded history", () => {
    let s: EditorState = createState(base());
    for (let i = 0; i < HISTORY_LIMIT + 10; i++) s = reduce(s, { type: "add_node", kind: "step" });
    expect(s.past).toHaveLength(HISTORY_LIMIT);
    const count = s.flow.nodes.length;
    s = reduce(s, { type: "undo" });
    expect(s.flow.nodes).toHaveLength(count - 1);
    expect(s.future).toHaveLength(1);
    s = reduce(s, { type: "redo" });
    expect(s.flow.nodes).toHaveLength(count);
    expect(reduce(s, { type: "redo" })).toBe(s);
  });

  it("coalesces rapid edits to the same field into one undo step", () => {
    const flow = base();
    const id = flow.nodes[0]?.id as string;
    let s = reduce(createState(flow), { type: "update_node", id, patch: { prompt: "H" }, coalesce: "prompt" }, 1000);
    s = reduce(s, { type: "update_node", id, patch: { prompt: "He" }, coalesce: "prompt" }, 1000 + COALESCE_MS / 2);
    s = reduce(s, { type: "update_node", id, patch: { prompt: "Hel" }, coalesce: "prompt" }, 1000 + COALESCE_MS - 1);
    expect(s.past).toHaveLength(1);
    s = reduce(s, { type: "update_node", id, patch: { name: "Renamed" }, coalesce: "name" }, 1000 + COALESCE_MS);
    expect(s.past).toHaveLength(2);
    s = reduce(s, { type: "undo" });
    expect(s.flow.nodes[0]?.name).toBe("Open");
    expect(s.flow.nodes[0]?.prompt).toBe("Hel");
    s = reduce(s, { type: "undo" });
    expect(s.flow.nodes[0]?.prompt).toBe("Hi.");
  });

  it("commits moves once and ignores no-op moves", () => {
    const flow = base();
    const id = flow.nodes[0]?.id as string;
    const s = reduce(createState(flow), { type: "move", positions: { [id]: { x: 10, y: 20 } } });
    expect(s.flow.nodes[0]?.position).toEqual({ x: 10, y: 20 });
    expect(s.past).toHaveLength(1);
    expect(reduce(s, { type: "move", positions: { [id]: { x: 10, y: 20 } } })).toBe(s);
  });

  it("drops a selection that no longer exists after undo", () => {
    let s = reduce(createState(base()), { type: "add_node", kind: "end" });
    expect(s.selection?.kind).toBe("node");
    s = reduce(s, { type: "undo" });
    expect(s.selection).toBeNull();
  });
});

describe("tidyUp", () => {
  it("lays nodes out left to right in rank order", () => {
    let s = reduce(createState(base()), { type: "add_node", kind: "step" });
    s = reduce(s, { type: "add_node", kind: "end" });
    const [open, step, end] = s.flow.nodes as [FlowNode, FlowNode, FlowNode];
    s = reduce(s, { type: "connect", from: open.id, to: step.id });
    s = reduce(s, { type: "connect", from: step.id, to: end.id });
    const tidy = tidyUp(s.flow);
    const x = (id: string) => tidy.nodes.find((n) => n.id === id)?.position.x ?? NaN;
    expect(x(open.id)).toBeLessThan(x(step.id));
    expect(x(step.id)).toBeLessThan(x(end.id));
    expect(tidy.paths).toBe(s.flow.paths);
  });
});
