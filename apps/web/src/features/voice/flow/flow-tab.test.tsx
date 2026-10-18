import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { useForm } from "react-hook-form";

import type { AgentForm } from "@/features/voice/agent-form";
import { FlowTab } from "@/features/voice/flow/flow-tab";
import { uuid, type FlowDefinition } from "@/features/voice/flow/types";

/**
 * The editor page resets the form to the fetched config *after* the tab mounts, so the
 * tab must seed its canvas from the server flow, not from whatever the form holds.
 */
function saved(): FlowDefinition {
  const start = uuid();
  const end = uuid();
  return {
    nodes: [
      {
        id: start,
        kind: "start",
        name: "Conversation",
        position: { x: 0, y: 0 },
        prompt: "Qualify the lead.",
        greeting: "Hi there",
        allow_interrupt: true,
        use_global_rules: true,
        capture_fields: [],
        action_ids: [],
        knowledge_ids: [],
      },
      {
        id: end,
        kind: "end",
        name: "Wrap up",
        position: { x: 600, y: 0 },
        prompt: "Thank them and say goodbye.",
        greeting: null,
        allow_interrupt: true,
        use_global_rules: false,
        capture_fields: [],
        action_ids: [],
        knowledge_ids: [],
      },
    ],
    paths: [{ id: uuid(), from_node: start, to_node: end, name: "Wrap up", when: "The conversation is over", say_while_moving: null }],
    viewport: null,
  };
}

function Harness({ initialFlow, formFlow }: { initialFlow: FlowDefinition; formFlow: FlowDefinition | null }) {
  // The form deliberately lags behind the server, as it does for one render in the page.
  const form = useForm<AgentForm>({ defaultValues: { mode: "flow", flow: formFlow, global_rules: "" } as unknown as AgentForm });
  return <FlowTab agentId="agent-1" version={2} initialFlow={initialFlow} form={form} canEdit dirty={false} focus={null} />;
}

describe("FlowTab", () => {
  it("seeds the canvas from the saved flow even when the form still holds nothing", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <Harness initialFlow={saved()} formFlow={null} />
      </QueryClientProvider>,
    );
    expect(screen.getByLabelText("Start Conversation")).toBeInTheDocument();
    expect(screen.getByLabelText("End Wrap up")).toBeInTheDocument();
    expect(screen.getByText(/^No problems/)).toBeInTheDocument();
  });
});
