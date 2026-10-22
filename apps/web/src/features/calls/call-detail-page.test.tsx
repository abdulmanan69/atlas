import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router";

import CallDetailPage from "@/features/calls/call-detail-page";
import type { CallDetail } from "@/features/queries";
import { session } from "@/lib/session";

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const call: CallDetail = {
  id: "c1",
  lead_id: "l1",
  lead_name: "Hina Khan",
  agent_id: "a1",
  agent_version: 3,
  campaign_id: null,
  direction: "outbound",
  mode: "simulated",
  state: "completed",
  outcome: "qualified",
  to_phone_e164: "+14155550100",
  from_phone_e164: null,
  started_at: "2026-10-18T10:00:00Z",
  ended_at: "2026-10-18T10:01:01Z",
  duration_seconds: 61,
  opted_out: false,
  recording_available: false,
  transcript_available: true,
  captured_count: 1,
  error_code: null,
  error_message: null,
  initiated_by_user_id: "u1",
  created_at: "2026-10-18T09:59:50Z",
  updated_at: "2026-10-18T10:01:01Z",
  captured: { budget: "500k" },
  summary: null,
  usage: { call_duration_seconds: 61 },
  estimated_cost: null,
};

function signIn() {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "A", created_at: "2026-01-01T00:00:00Z" },
    memberships: [
      {
        id: "m1",
        role: "owner",
        status: "active",
        organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "US", created_at: "2026-01-01T00:00:00Z" },
      },
    ],
  } as never);
}

function stubApi(overrides: Partial<CallDetail> = {}, transcript = "[SIMULATED CALL]\nAgent: Hi Hina, is now a good time?\nHina: Sure, go ahead.") {
  const fetchMock = vi.fn(async (input: Request | string) => {
    const url = new URL(typeof input === "string" ? input : input.url);
    switch (url.pathname) {
      case "/api/v1/calls/c1":
        return json({ ...call, ...overrides });
      case "/api/v1/calls/c1/transcript":
        return json({ call_id: "c1", mode: "simulated", simulated: true, text: transcript });
      case "/api/v1/calls/c1/events":
        return json([{ id: "e1", type: "state_changed", data: { from: "queued", to: "completed" }, occurred_at: "2026-10-18T10:01:01Z" }]);
      case "/api/v1/agents":
        return json([{ id: "a1", name: "Qualifier", description: null, direction: "outbound", status: "active", latest_version: 3, published_version: 3, sync_status: "synced", sync_error: null, synced_at: null, created_at: "2026-01-01T00:00:00Z", updated_at: "2026-01-01T00:00:00Z" }]);
      case "/api/v1/calls/missing":
        return json({ error: { code: "not_found", message: "Call not found" } }, 404);
      default:
        return json({ error: { code: "not_found", message: `Unexpected ${url.pathname}` } }, 404);
    }
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderPage(id = "c1") {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter initialEntries={[`/calls/${id}`]}>
        <Routes>
          <Route path="/calls/:callId" element={<CallDetailPage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("CallDetailPage", () => {
  beforeEach(signIn);
  afterEach(() => vi.unstubAllGlobals());

  it("renders the simulated badge with its explanation and the transcript as turns", async () => {
    stubApi();
    renderPage();
    expect(await screen.findByRole("heading", { name: "Hina Khan" })).toBeInTheDocument();
    expect(screen.getByText("Simulated")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Simulated call. No phone call was placed.");
    // Header badges and the Result card both show state and outcome.
    expect(screen.getAllByText("Answered")).toHaveLength(2);
    expect(screen.getAllByText("Qualified")).toHaveLength(2);

    const conversation = await screen.findByRole("list", { name: "Conversation" });
    const turns = within(conversation).getAllByRole("listitem");
    expect(turns).toHaveLength(3);
    expect(turns[0]).toHaveTextContent("[SIMULATED CALL]");
    expect(turns[1]).toHaveAttribute("data-role", "agent");
    expect(turns[1]).toHaveTextContent("Agent");
    expect(turns[1]).toHaveTextContent("Hi Hina, is now a good time?");
    expect(turns[2]).toHaveAttribute("data-role", "person");
    expect(turns[2]).toHaveTextContent("Hina");
    expect(turns[2]).toHaveTextContent("Sure, go ahead.");

    expect(screen.getByText("budget")).toBeInTheDocument();
    expect(screen.getByText("500k")).toBeInTheDocument();
    expect(screen.queryByText("Recording")).not.toBeInTheDocument();
    expect(await screen.findByText("State changed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Open lead/ })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Hina Khan" })).toHaveAttribute("href", "/leads/l1");
  });

  it("shows the sounds an expressive voice made as chips, not brackets", async () => {
    stubApi({}, "[2026-10-22T10:00:00.000+00:00] assistant: [laugh] That's a good one.\n[2026-10-22T10:00:04.000+00:00] user: Ha [inaudible].");
    renderPage();
    const conversation = await screen.findByRole("list", { name: "Conversation" });
    const [agent, caller] = within(conversation).getAllByRole("listitem");
    const chip = within(agent!).getByTitle("A sound the voice made");
    expect(chip).toHaveTextContent("laughs");
    expect(agent).not.toHaveTextContent("[laugh]");
    expect(agent).toHaveTextContent("That's a good one.");
    // Other bracketed text is kept as written.
    expect(caller).toHaveTextContent("Ha [inaudible].");
    expect(within(caller!).queryByTitle("A sound the voice made")).not.toBeInTheDocument();
  });

  it("explains a live call without the simulated note and shows the recording card", async () => {
    stubApi({ mode: "live", recording_available: true });
    renderPage();
    expect((await screen.findAllByText("Live")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Simulated")).not.toBeInTheDocument();
    expect(screen.queryByRole("note")).not.toBeInTheDocument();
    expect(screen.getByText("Recording")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Play" })).toBeInTheDocument();
  });

  it("marks a browser test call as not a real phone call", async () => {
    stubApi({ mode: "test", direction: "inbound", lead_id: null, lead_name: null, to_phone_e164: null, outcome: null });
    renderPage();
    expect(await screen.findByRole("heading", { name: "Browser test call" })).toBeInTheDocument();
    const note = screen.getByRole("note");
    expect(note).toHaveTextContent("Browser test call — not a real phone call.");
    expect(note).not.toHaveTextContent("the lead was not contacted");
    expect(screen.getByText("Test")).toBeInTheDocument();
    expect(screen.getByText("Browser")).toBeInTheDocument();
    expect(screen.queryByText("Simulated")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Open lead/ })).not.toBeInTheDocument();
  });

  it("says the lead only filled the variables of a test call", async () => {
    stubApi({ mode: "test", direction: "inbound", to_phone_e164: null });
    renderPage();
    expect(await screen.findByRole("heading", { name: "Hina Khan" })).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("The lead's details filled the variables; the lead was not contacted.");
  });

  it("shows a not-found state for an unknown call", async () => {
    stubApi();
    renderPage("missing");
    expect(await screen.findByRole("alert")).toHaveTextContent("Call not found");
  });
});
