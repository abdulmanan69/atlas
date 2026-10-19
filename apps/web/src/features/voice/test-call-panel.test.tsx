import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router";

import { TestCallPanel } from "@/features/voice/test-call-panel";
import { session } from "@/lib/session";
import type { CallEvents } from "@/lib/webrtc";

// The browser call itself is covered in lib/webrtc.test.ts; here only its events matter.
const calls: CallEvents[] = [];
vi.mock("@/lib/webrtc", () => ({
  BrowserCall: class {
    constructor(_session: unknown, events: CallEvents) {
      calls.push(events);
    }
    start() {
      return Promise.resolve();
    }
    hangup() {}
    setMuted() {}
  },
}));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

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

function stubApi() {
  const ends: unknown[] = [];
  const fetchMock = vi.fn(async (input: Request | string) => {
    const request = typeof input === "string" ? new Request(input) : input;
    const url = new URL(request.url);
    if (url.pathname === "/api/v1/leads") return json({ items: [], total: 0, page: 1, page_size: 50 });
    if (url.pathname === "/api/v1/agents/a1/test-session") {
      return json({ session_token: "s", signaling_url: "ws://x/signaling/s", ice_servers: [], expires_in_seconds: 3600, variables: {}, call_id: "c1" });
    }
    if (url.pathname === "/api/v1/agents/a1/test-calls/c1/end") {
      ends.push(await request.json());
      return json({ id: "c1", state: "in_progress" });
    }
    return json({ error: { code: "not_found", message: `Unexpected ${url.pathname}` } }, 404);
  });
  vi.stubGlobal("fetch", fetchMock);
  return ends;
}

function renderPanel() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
      <MemoryRouter>
        <TestCallPanel agentId="a1" published />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("TestCallPanel", () => {
  beforeEach(() => {
    calls.length = 0;
    signIn();
  });
  afterEach(() => vi.unstubAllGlobals());

  it("links to the call record while it runs and reports how it ended", async () => {
    const ends = stubApi();
    renderPanel();
    expect(screen.queryByRole("link", { name: /View call details/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Start test call/ }));
    await waitFor(() => expect(calls).toHaveLength(1));
    act(() => calls[0]!.onState("live"));
    // While live, the record opens beside the call: leaving the page would hang up.
    const running = screen.getByRole("link", { name: /View call details/ });
    expect(running).toHaveAttribute("href", "/calls/c1");
    expect(running).toHaveAttribute("target", "_blank");

    act(() => calls[0]!.onState("ended", "You ended the call."));
    await waitFor(() => expect(ends).toEqual([{ connected: true }]));
    const after = screen.getByRole("link", { name: /View call details/ });
    expect(after).toHaveAttribute("href", "/calls/c1");
    expect(after).not.toHaveAttribute("target");
    expect(screen.getByText(/transcript and response times appear there/)).toBeInTheDocument();
  });

  it("reports a call that never connected, once", async () => {
    const ends = stubApi();
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Start test call/ }));
    await waitFor(() => expect(calls).toHaveLength(1));
    act(() => calls[0]!.onState("requesting-mic"));
    act(() => calls[0]!.onState("failed", "Microphone access was blocked."));
    act(() => calls[0]!.onState("ended"));
    await waitFor(() => expect(ends).toEqual([{ connected: false }]));
    expect(screen.getByRole("link", { name: /View call details/ })).toHaveAttribute("href", "/calls/c1");
    expect(screen.getByText(/never connected/)).toBeInTheDocument();
  });
});
