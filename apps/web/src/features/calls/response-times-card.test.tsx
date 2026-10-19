import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import type { CallLatencyDetail, LatencySummary } from "@/features/calls/latency-types";
import { ResponseTimesCard } from "@/features/calls/response-times-card";
import { session } from "@/lib/session";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const summary: LatencySummary = {
  source: "runtime",
  turns: 2,
  measured_turns: 2,
  response_p50_ms: 700,
  response_p95_ms: 1900,
  response_max_ms: 1900,
  first_response_ms: 700,
  stt_ttfb_p50_ms: 120,
  llm_ttfb_p50_ms: 400,
  tts_ttfb_p50_ms: 150,
  tool_time_ms: 600,
  slow_turns: 1,
  interruptions: 0,
};

const detail: CallLatencyDetail = {
  summary,
  thresholds: { target_ms: 800, slow_ms: 1500 },
  turns: [
    { turn: 1, node_name: "Greeting", user_text: "Hello?", agent_text: "Hi", user_started_at: null, user_ended_at: null, agent_started_at: null, agent_ended_at: null, response_ms: 700, stt_ttfb_ms: 100, llm_ttfb_ms: 400, tts_ttfb_ms: 150, tools: [], slow: false },
    {
      turn: 2,
      node_name: "Answer",
      user_text: "What are your hours?",
      agent_text: "We open at nine",
      user_started_at: null,
      user_ended_at: null,
      agent_started_at: null,
      agent_ended_at: null,
      response_ms: 1900,
      stt_ttfb_ms: 150,
      llm_ttfb_ms: 500,
      tts_ttfb_ms: 200,
      tools: [{ name: "retrieve_from_knowledge_base", kind: "knowledge", duration_ms: 600 }],
      slow: true,
    },
  ],
};

function signIn() {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "A", created_at: "2026-01-01T00:00:00Z" },
    memberships: [{ id: "m1", role: "owner", status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "US", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
}

const renderCard = (props: Partial<Parameters<typeof ResponseTimesCard>[0]> = {}) =>
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <ResponseTimesCard callId="c1" summary={summary} simulated={false} {...props} />
    </QueryClientProvider>,
  );

describe("ResponseTimesCard", () => {
  beforeEach(signIn);
  afterEach(() => vi.unstubAllGlobals());

  it("shows the summary, then loads the per-turn waterfall on demand with slow turns marked", async () => {
    const fetchMock = vi.fn(async (input: Request | string) => {
      const url = new URL(typeof input === "string" ? input : input.url);
      return url.pathname === "/api/v1/calls/c1/latency" ? json(detail) : json({ error: { code: "not_found", message: "nope" } }, 404);
    });
    vi.stubGlobal("fetch", fetchMock);
    renderCard();

    const tiles = screen.getByLabelText("Response time summary");
    expect(tiles).toHaveTextContent("Median response");
    expect(tiles).toHaveTextContent("700 ms");
    expect(tiles).toHaveTextContent("1.9 s");
    expect(screen.getByText("1 slow reply")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: /Show each turn/ }));
    const list = await screen.findByRole("list", { name: "Turns" });
    const rows = within(list).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(rows[0]).not.toHaveAttribute("data-slow");
    expect(rows[1]).toHaveAttribute("data-slow", "true");
    expect(within(rows[1]!).getByLabelText("Knowledge lookup 600 ms")).toBeInTheDocument();
    expect(rows[1]).toHaveTextContent("1.9 s");
  });

  it("says when a call was not measured, and labels synthetic timings", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ error: { code: "not_found", message: "Not measured" } }, 404)));
    const { unmount } = renderCard({ summary: null });
    expect(screen.getByText("Not measured for this call")).toBeInTheDocument();
    unmount();

    renderCard({ summary: { ...summary, source: "simulated" }, simulated: true });
    expect(screen.getAllByText("Synthetic timings from the dry run").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("button", { name: /Show each turn/ }));
    expect(await screen.findByText("Not measured for this call")).toBeInTheDocument();
  });
});
