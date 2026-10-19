import { estimateTokens, issuesByTarget, pathFunctionName, PROMPT_BUDGET, validateFlow } from "@/features/voice/flow/validate";
import { uuid, type FlowDefinition, type FlowNode, type FlowPath, type FlowValidation, type NodeKind } from "@/features/voice/flow/types";

function node(kind: NodeKind, name: string, patch: Partial<FlowNode> = {}): FlowNode {
  return {
    id: uuid(),
    kind,
    name,
    position: { x: 0, y: 0 },
    prompt: `Do ${name}.`,
    greeting: kind === "start" ? "Hello there" : null,
    allow_interrupt: true,
    use_global_rules: true,
    capture_fields: [],
    action_ids: [],
    knowledge_ids: [],
    ...patch,
  };
}

function path(from: FlowNode, to: FlowNode, name: string, when = "When it applies"): FlowPath {
  return { id: uuid(), from_node: from.id, to_node: to.id, name, when, say_while_moving: null };
}

const keys = (v: FlowValidation, kind: "errors" | "warnings" = "errors") => new Set(v[kind].map((i) => `${i.target}:${i.id}:${i.field ?? ""}`));
const linear = () => {
  const start = node("start", "Open");
  const step = node("step", "Qualify");
  const end = node("end", "Done");
  const flow: FlowDefinition = { nodes: [start, step, end], paths: [path(start, step, "Continue"), path(step, end, "Finish")], viewport: null };
  return { flow, start, step, end };
};

describe("pathFunctionName", () => {
  it("normalises like the voice service", () => {
    expect(pathFunctionName("Book Now")).toBe("book_now");
    expect(pathFunctionName("book-now")).toBe("book_now");
    expect(pathFunctionName("Déjà vu!")).toBe("d_j__vu_");
  });
});

describe("validateFlow", () => {
  it("accepts a branching flow and warns about its cycle", () => {
    const start = node("start", "Open");
    const qualify = node("step", "Qualify");
    const book = node("step", "Book");
    const booked = node("end", "Booked");
    const later = node("end", "Later");
    const retry = path(book, qualify, "Rethink");
    const flow: FlowDefinition = {
      nodes: [start, qualify, book, booked, later],
      paths: [path(start, qualify, "Ready"), path(start, later, "Bad time"), path(qualify, book, "Fit"), path(qualify, later, "No fit"), path(book, booked, "Booked"), retry],
      viewport: null,
    };
    const v = validateFlow(flow);
    expect(v.valid).toBe(true);
    expect(keys(v, "warnings")).toEqual(new Set([`path:${retry.id}:`]));
  });

  it("requires exactly one Start and at least one End", () => {
    const end = node("end", "Done");
    const none = validateFlow({ nodes: [end], paths: [], viewport: null });
    expect(none.errors.some((e) => e.target === "flow" && e.message.includes("Start"))).toBe(true);
    const a = node("start", "A");
    const b = node("start", "B");
    const two = validateFlow({ nodes: [a, b, end], paths: [path(a, end, "x"), path(b, end, "y")], viewport: null });
    expect(keys(two)).toEqual(new Set([`node:${b.id}:`]));
    const noEnd = validateFlow({ nodes: [a], paths: [], viewport: null });
    expect(noEnd.errors.some((e) => e.message.includes("End"))).toBe(true);
  });

  it("forbids paths into Start and out of an End", () => {
    const { flow, start, step, end } = linear();
    const back = path(step, start, "Back");
    const out = path(end, step, "Again");
    const v = validateFlow({ ...flow, paths: [...flow.paths, back, out] });
    expect(keys(v)).toEqual(new Set([`path:${back.id}:to_node`, `path:${out.id}:from_node`]));
  });

  it("requires a path out of every Start and Step", () => {
    const start = node("start", "S");
    const dead = node("step", "Dead");
    const end = node("end", "E");
    const v = validateFlow({ nodes: [start, dead, end], paths: [path(start, end, "Go")], viewport: null });
    expect(keys(v)).toEqual(new Set([`node:${dead.id}:paths`, `node:${dead.id}:`]));
  });

  it("flags unreachable nodes", () => {
    const { flow, end } = linear();
    const island = node("step", "Island");
    const v = validateFlow({ ...flow, nodes: [...flow.nodes, island], paths: [...flow.paths, path(island, end, "x")] });
    expect(keys(v)).toEqual(new Set([`node:${island.id}:`]));
  });

  it("requires unique path names per source after normalisation", () => {
    const start = node("start", "S");
    const a = node("end", "A");
    const b = node("end", "B");
    const p1 = path(start, a, "Book Now");
    const p2 = path(start, b, "book-now");
    expect(keys(validateFlow({ nodes: [start, a, b], paths: [p1, p2], viewport: null }))).toEqual(new Set([`path:${p1.id}:name`, `path:${p2.id}:name`]));
    const mid = node("step", "Mid");
    expect(validateFlow({ nodes: [start, mid, a], paths: [path(start, mid, "Next"), path(mid, a, "Next")], viewport: null }).valid).toBe(true);
  });

  it("stops a path shadowing an action on its source node", () => {
    const start = node("start", "S", { action_ids: ["act-1"] });
    const end = node("end", "E");
    const p = path(start, end, "Book viewing");
    const flow: FlowDefinition = { nodes: [start, end], paths: [p], viewport: null };
    expect(keys(validateFlow(flow, { "act-1": "book_viewing" }))).toEqual(new Set([`path:${p.id}:name`]));
    expect(validateFlow(flow, { "act-2": "book_viewing" }).valid).toBe(true);
  });

  it("requires unique node names", () => {
    const start = node("start", "S");
    const a = node("end", "Done");
    const b = node("end", "done ");
    const v = validateFlow({ nodes: [start, a, b], paths: [path(start, a, "x"), path(start, b, "y")], viewport: null });
    expect(keys(v)).toEqual(new Set([`node:${b.id}:name`]));
  });

  it("reports blank prompt, greeting and condition with ids and fields", () => {
    const start = node("start", "S", { prompt: "  ", greeting: " " });
    const end = node("end", "E", { prompt: "" });
    const p = path(start, end, "Go", "   ");
    const v = validateFlow({ nodes: [start, end], paths: [p], viewport: null });
    expect(keys(v)).toEqual(new Set([`node:${start.id}:prompt`, `node:${start.id}:greeting`, `node:${end.id}:prompt`, `path:${p.id}:when`]));
  });

  it("warns on self-loops and single-path steps", () => {
    const { flow, step } = linear();
    const loop = path(step, step, "Again");
    const v = validateFlow({ ...flow, paths: [...flow.paths, loop] });
    expect(v.valid).toBe(true);
    expect(v.warnings.find((w) => w.id === loop.id)?.message).toContain("same step");
    expect(keys(validateFlow(flow), "warnings")).toEqual(new Set([`node:${step.id}:paths`]));
  });
});

describe("issuesByTarget", () => {
  it("groups by target and id", () => {
    const grouped = issuesByTarget([
      { target: "node", id: "a", field: "prompt", message: "x" },
      { target: "node", id: "a", field: "name", message: "y" },
      { target: "path", id: "a", field: null, message: "z" },
    ]);
    expect(grouped.get("node:a")).toHaveLength(2);
    expect(grouped.get("path:a")).toHaveLength(1);
  });
});

describe("prompt budget", () => {
  it("estimates about four characters per token", () => {
    expect(estimateTokens("")).toBe(0);
    expect(estimateTokens("   ")).toBe(0);
    expect(estimateTokens("abcd")).toBe(1);
    expect(estimateTokens("abcde")).toBe(2);
  });

  it("warns about a long node prompt and long global rules, without blocking publishing", () => {
    const { flow, step } = linear();
    step.prompt = "x".repeat(PROMPT_BUDGET.node * 4 + 4);
    const v = validateFlow(flow, {}, "r".repeat(PROMPT_BUDGET.globalRules * 4 + 4));
    expect(v.valid).toBe(true);
    const budget = (x: FlowValidation) => x.warnings.filter((i) => i.field === "prompt" || i.field === "global_rules");
    expect(budget(v).map((i) => `${i.target}:${i.id}:${i.field}`)).toEqual([`node:${step.id}:prompt`, "flow::global_rules"]);
    expect(budget(v)[0]?.message).toContain("1,501 tokens");
    expect(budget(validateFlow(linear().flow, {}, "short rules"))).toHaveLength(0);
  });
});
