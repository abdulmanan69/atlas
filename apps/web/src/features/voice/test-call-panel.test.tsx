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
const sse = (...events: unknown[]) =>
  new Response(": keep-alive\n\n" + events.map((e) => `data: ${JSON.stringify(e)}\n\n`).join(""), { status: 200, headers: { "Content-Type": "text/event-stream" } });

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

  it("shows the live conversation and each reply's response time during a call", async () => {
    stubApi();
    renderPanel();
    fireEvent.click(screen.getByRole("button", { name: /Start test call/ }));
    await waitFor(() => expect(calls).toHaveLength(1));
    act(() => calls[0]!.onState("live"));
    const feed = (type: string, payload: Record<string, unknown> = {}) => act(() => calls[0]!.onFeedback?.({ type, payload, node_name: "Conversation" }));
    feed("rtf-node-transition", { node_name: "Conversation" });
    feed("rtf-bot-started-speaking");
    feed("rtf-bot-text", { text: "Hi, this is Sarah." });
    feed("rtf-bot-stopped-speaking");
    feed("rtf-user-transcription", { text: "Is it exp", final: false });
    expect(screen.getByText("Is it exp")).toHaveClass("italic");
    feed("rtf-user-transcription", { text: "Is it expensive?", final: true });
    feed("rtf-ttfb-metric", { ttfb_seconds: 0.31, kind: "llm" });
    feed("rtf-bot-started-speaking");
    feed("rtf-latency-measured", { latency_seconds: 0.62 });
    feed("rtf-function-call-start", { function_name: "retrieve_from_knowledge_base", tool_call_id: "k1" });

    const log = screen.getByRole("log", { name: "Conversation" });
    expect(log).toHaveTextContent("Step · Conversation");
    expect(log).toHaveTextContent("Hi, this is Sarah.");
    expect(screen.queryByText("Is it exp")).not.toBeInTheDocument();
    expect(log).toHaveTextContent("Is it expensive?");
    expect(screen.getByLabelText(/^Response 620 ms, Language model, first words 310 ms$/)).toHaveClass("text-success");
    expect(log).toHaveTextContent("Knowledge lookup");
  });

  it("switches to typing and chats with the published agent", async () => {
    const sent: { path: string; body: unknown }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: Request | string) => {
        const request = typeof input === "string" ? new Request(input) : input;
        const url = new URL(request.url);
        const body = request.method === "POST" ? await request.text() : "";
        sent.push({ path: url.pathname, body: body ? JSON.parse(body) : null });
        if (url.pathname === "/api/v1/leads") return json({ items: [], total: 0, page: 1, page_size: 50 });
        if (url.pathname === "/api/v1/agents/a1/test-chat") {
          return sse(
            { type: "started", at_ms: 2, call_id: "c9" },
            { type: "step", at_ms: 30, name: "Conversation" },
            { type: "agent_text", at_ms: 1180, text: "Hi Alex, this is Sarah." },
            { type: "done", at_ms: 1190, ended: false },
          );
        }
        if (url.pathname === "/api/v1/agents/a1/test-chat/c9/messages") {
          return sse({ type: "agent_text", at_ms: 640, text: "Plans start at 49 dollars." }, { type: "done", at_ms: 650, ended: false });
        }
        if (url.pathname === "/api/v1/agents/a1/test-chat/c9/end") return json({ id: "c9", state: "in_progress", channel: "text" });
        return json({ error: { code: "not_found", message: `Unexpected ${url.pathname}` } }, 404);
      }),
    );
    renderPanel();
    fireEvent.click(screen.getByRole("radio", { name: "Type" }));
    expect(screen.getByRole("radio", { name: "Type" })).toHaveAttribute("aria-checked", "true");
    expect(screen.queryByRole("button", { name: /Start test call/ })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Start test chat/ }));
    const log = screen.getByRole("log", { name: "Conversation" });
    await waitFor(() => expect(log).toHaveTextContent("Hi Alex, this is Sarah."));
    expect(screen.getByLabelText(/^Reply 1\.2 s$/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /View call details/ })).toHaveAttribute("href", "/calls/c9");
    // One test at a time: the other mode is locked while the chat is open.
    expect(screen.getByRole("radio", { name: "Speak" })).toBeDisabled();

    const box = screen.getByRole("textbox", { name: "Message to the agent" });
    await waitFor(() => expect(box).not.toBeDisabled());
    fireEvent.change(box, { target: { value: "How much is it?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(log).toHaveTextContent("Plans start at 49 dollars."));
    expect(log).toHaveTextContent("How much is it?");
    expect(sent.find((r) => r.path.endsWith("/messages"))?.body).toEqual({ text: "How much is it?" });
    expect(screen.getByLabelText(/^Reply 640 ms$/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /End chat/ }));
    await waitFor(() => expect(sent.some((r) => r.path === "/api/v1/agents/a1/test-chat/c9/end")).toBe(true));
    expect(screen.getByRole("button", { name: /New test chat/ })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Speak" })).not.toBeDisabled();
    expect(sent.find((r) => r.path === "/api/v1/agents/a1/test-chat")?.body).toEqual({ lead_id: null });
  });

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
