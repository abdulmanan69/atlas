import { ReactFlowProvider } from "@xyflow/react";
import { render, screen } from "@testing-library/react";

import { FlowCanvas } from "@/features/voice/flow/canvas";
import { createState, reduce } from "@/features/voice/flow/model";
import { uuid, type FlowNode } from "@/features/voice/flow/types";
import { issuesByTarget, validateFlow } from "@/features/voice/flow/validate";

function start(): FlowNode {
  return {
    id: uuid(),
    kind: "start",
    name: "Open the call",
    position: { x: 0, y: 0 },
    prompt: "Find out whether now is a good time.",
    greeting: "Hi there",
    allow_interrupt: true,
    use_global_rules: true,
    capture_fields: [],
    action_ids: [],
    knowledge_ids: [],
  };
}

describe("FlowCanvas", () => {
  it("renders three nodes with their names, excerpts and a path label", () => {
    let s = reduce(createState({ nodes: [start()], paths: [], viewport: null }), { type: "add_node", kind: "step" });
    s = reduce(s, { type: "add_node", kind: "end" });
    const [open, step, end] = s.flow.nodes as [FlowNode, FlowNode, FlowNode];
    s = reduce(s, { type: "update_node", id: step.id, patch: { name: "Qualify", prompt: "Ask about budget and timeline.", capture_fields: [{ name: "budget", type: "string", description: "Budget" }] } });
    s = reduce(s, { type: "update_node", id: end.id, patch: { name: "Booked", prompt: "Confirm and say goodbye." } });
    s = reduce(s, { type: "connect", from: open.id, to: step.id });
    s = reduce(s, { type: "connect", from: step.id, to: end.id });
    s = reduce(s, { type: "update_path", id: s.flow.paths[0]?.id as string, patch: { name: "Ready to talk", when: "They have a moment" } });
    const v = validateFlow(s.flow);
    const dispatch = vi.fn();

    render(
      <ReactFlowProvider>
        <div style={{ width: 800, height: 600 }}>
          <FlowCanvas agentId="agent-1" state={s} dispatch={dispatch} errors={issuesByTarget(v.errors)} warnings={issuesByTarget(v.warnings)} snap readOnly={false} />
        </div>
      </ReactFlowProvider>,
    );

    expect(screen.getByText("Open the call")).toBeInTheDocument();
    expect(screen.getByText("Qualify")).toBeInTheDocument();
    expect(screen.getByText("Booked")).toBeInTheDocument();
    expect(screen.getByText("Ask about budget and timeline.")).toBeInTheDocument();
    expect(screen.getByText("Hi there")).toBeInTheDocument();
    expect(screen.getByText("Start")).toBeInTheDocument();
    expect(screen.getByText("End")).toBeInTheDocument();
    // jsdom has no layout, so React Flow never measures nodes and draws no edges here; the
    // path pill is covered by the edge component's own markup, not this smoke test.
    expect(screen.getByLabelText("Flow canvas")).toBeInTheDocument();
    expect(screen.getByLabelText("Step Qualify")).toBeInTheDocument();
    // The second path still needs a condition: the step shows its warning, the path its error.
    expect(screen.getByTitle("1 warning")).toBeInTheDocument();
  });
});
