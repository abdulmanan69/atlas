import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import AiProvidersPage from "@/features/voice/ai-providers-page";
import type { AiServer, AiServerRegistered } from "@/features/voice/ai-servers";
import { AiServersCard } from "@/features/voice/ai-servers-section";
import { session } from "@/lib/session";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

function renderWithProviders(ui: React.ReactElement) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

function signInAs(role: "owner" | "admin" | "manager") {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
    memberships: [{ id: "m1", role, status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
}

/** Answers by method and path; records every request. */
function route(handlers: Record<string, (req: Request) => Response | Promise<Response>>) {
  const calls: Request[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (req: Request) => {
      calls.push(req.clone());
      const key = `${req.method} ${new URL(req.url).pathname}`;
      const handler = handlers[key];
      if (!handler) throw new Error(`unexpected request ${key}`);
      return handler(req);
    }),
  );
  return calls;
}

const bodyOf = async (calls: Request[], method: string, path: string) => (await calls.find((r) => r.method === method && new URL(r.url).pathname === path)!.clone().json()) as unknown;

const SERVER: AiServer = {
  id: "s1",
  name: "GPU box",
  url: "https://ai.203-0-113-7.sslip.io",
  status: "degraded",
  last_checked_at: new Date(Date.now() - 120_000).toISOString(),
  last_latency_ms: 132,
  last_error: null,
  live_stt_secret_set: true,
  created_at: "2026-10-22T10:00:00Z",
  updated_at: "2026-10-22T10:00:00Z",
  services: [
    { service: "llm", title: "Language model", state: "up", up: true, latency_ms: 4, models: ["qwen2.5:1.5b", "llama3.2:3b"], loaded: ["qwen2.5:1.5b"], connection_id: "c-llm" },
    { service: "whisper", title: "Whisper", state: "down", up: false, error: "timeout", models: ["Systran/faster-whisper-base"], connection_id: "c-whisper" },
    { service: "tts", title: "Pocket TTS", state: "up", up: true, latency_ms: 2, models: ["pocket-tts"], voices: 20, default_voice: "alba", connection_id: "c-tts" },
    { service: "live_stt", title: "Live speech", state: "up", up: true, latency_ms: 2, models: ["parakeet-en-110m"], sessions: 1, max_sessions: 4, connection_id: "c-live" },
  ],
};

const option = (id: string) => ({ id, title: id, self_hosted: id === "speaches", credential_fields: ["api_key"], connection_fields: [], credential_required: false, settings_schema: {} });
const CATALOG = { llm: [option("openai"), option("speaches")], stt: [option("deepgram"), option("speaches")], tts: [option("speaches")], embeddings: [] };
const conn = (id: string, name: string, provider: string) => ({ id, name, provider, provider_title: provider, connection_settings: {}, configured_credentials: ["api_key"], hosted_trial: false });
const CONNECTIONS = [conn("c-openai", "OpenAI", "openai"), conn("c-llm", "GPU box · Language model", "speaches"), conn("c-whisper", "GPU box · Whisper", "speaches"), conn("c-tts", "GPU box · Pocket TTS", "speaches"), conn("c-live", "GPU box · Live speech", "deepgram")];
const MODELS = { llm: { connection_id: "c-openai", settings: { model: "gpt-4.1-mini" } }, stt: null, tts: null, embeddings: null, llm_fallback: null };

afterEach(() => {
  vi.unstubAllGlobals();
  signInAs("admin");
});

describe("AI servers", () => {
  it("shows each server's status, address, last check and services, read-only for managers", async () => {
    signInAs("manager");
    route({ "GET /api/v1/ai/servers": () => json(200, [SERVER]) });
    renderWithProviders(<AiServersCard canEdit={false} />);
    expect(await screen.findByText("GPU box")).toBeInTheDocument();
    expect(screen.getByText("https://ai.203-0-113-7.sslip.io")).toBeInTheDocument();
    expect(screen.getByText("Some services down")).toBeInTheDocument();
    expect(screen.getByText("Checked 2 minutes ago")).toBeInTheDocument();
    expect(screen.getByText("132 ms round trip")).toBeInTheDocument();
    const services = within(screen.getByRole("list", { name: "Services of GPU box" }));
    expect(services.getByText("qwen2.5:1.5b · llama3.2:3b")).toBeInTheDocument();
    expect(services.getByText("Down (timeout)")).toBeInTheDocument();
    expect(services.getByText("pocket-tts · 20 voices")).toBeInTheDocument();
    expect(services.getByText("parakeet-en-110m · 1 of 4 calls")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Check now/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Add AI server/ })).not.toBeInTheDocument();
  });

  it("adds a server, showing the API's field errors and what was set up", async () => {
    let attempts = 0;
    const registered: AiServerRegistered = {
      ...SERVER,
      status: "ok",
      setup: [
        { service: "llm", created: true, connection_id: "c-llm", connection_name: "GPU box · Language model" },
        { service: "whisper", created: true, connection_id: "c-whisper", connection_name: "GPU box · Whisper" },
        { service: "tts", created: true, connection_id: "c-tts", connection_name: "GPU box · Pocket TTS" },
        { service: "live_stt", created: false, reason: "Give a Deepgram key to use it." },
      ],
    };
    const calls = route({
      "GET /api/v1/ai/servers": () => json(200, []),
      "POST /api/v1/ai/servers": () =>
        ++attempts === 1
          ? json(422, { error: { code: "validation_failed", message: "The server refused this token.", details: { field: "token" } } })
          : json(201, registered),
    });
    renderWithProviders(<AiServersCard canEdit />);
    expect(await screen.findByText("No AI servers yet")).toBeInTheDocument();
    await userEvent.click(screen.getAllByRole("button", { name: /Add AI server/ })[0]!);

    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/checks every Deepgram-type key with Deepgram's cloud when it saves the connection/)).toBeInTheDocument();
    const add = dialog.getByRole("button", { name: "Check and add" });
    expect(add).toBeDisabled();
    await userEvent.type(dialog.getByLabelText(/^Name/), "GPU box");
    await userEvent.type(dialog.getByLabelText(/^Address/), "https://ai.203-0-113-7.sslip.io");
    const token = dialog.getByLabelText(/^Token/);
    expect(token).toHaveAttribute("type", "password");
    await userEvent.type(token, "wrong-token");
    await userEvent.type(dialog.getByLabelText(/^Live speech-to-text secret/), "ls-secret");
    await userEvent.click(add);
    expect(await dialog.findByText("The server refused this token.")).toBeInTheDocument();
    expect(token).toHaveAttribute("aria-invalid", "true");
    expect(await bodyOf(calls, "POST", "/api/v1/ai/servers")).toEqual({
      name: "GPU box",
      url: "https://ai.203-0-113-7.sslip.io",
      token: "wrong-token",
      live_stt_secret: "ls-secret",
      deepgram_key: null,
    });

    await userEvent.clear(token);
    await userEvent.type(token, "right-token");
    await userEvent.click(add);
    expect(await screen.findByRole("heading", { name: "Added GPU box" })).toBeInTheDocument();
    expect(screen.getByText("Added as GPU box · Pocket TTS")).toBeInTheDocument();
    expect(screen.getByText("Not added. Give a Deepgram key to use it.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Use for this organization" }));
    expect(await screen.findByRole("heading", { name: "Use GPU box for this organization" })).toBeInTheDocument();
  });

  it("uses a server with the chosen model, Whisper and a fallback model", async () => {
    const calls = route({
      "GET /api/v1/ai/servers": () => json(200, [SERVER]),
      "GET /api/v1/ai/catalog": () => json(200, CATALOG),
      "GET /api/v1/ai/connections": () => json(200, CONNECTIONS),
      "GET /api/v1/ai/models": () => json(200, MODELS),
      "POST /api/v1/ai/servers/s1/use": () => json(200, MODELS),
    });
    renderWithProviders(<AiServersCard canEdit />);
    await userEvent.click(await screen.findByRole("button", { name: "Use for this organization" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByText(/In memory now: qwen2.5:1.5b/)).toBeInTheDocument();

    const model = dialog.getByRole("combobox", { name: /Language model/ });
    expect(model).toHaveTextContent("qwen2.5:1.5b");
    model.focus();
    await userEvent.keyboard("{Enter}");
    await userEvent.click(await screen.findByRole("option", { name: "llama3.2:3b" }));
    await userEvent.click(dialog.getByRole("checkbox", { name: "Use live speech-to-text" }));

    const fallback = dialog.getByRole("combobox", { name: /Fallback language model/ });
    fallback.focus();
    await userEvent.keyboard("{Enter}");
    const listbox = within(await screen.findByRole("listbox"));
    expect(listbox.queryByRole("option", { name: /GPU box/ })).not.toBeInTheDocument();
    await userEvent.click(listbox.getByRole("option", { name: /OpenAI/ }));
    expect(await dialog.findByPlaceholderText("Same as now: gpt-4.1-mini")).toBeInTheDocument();

    await userEvent.click(dialog.getByRole("button", { name: "Use GPU box" }));
    await waitFor(() => expect(calls.some((r) => r.method === "POST")).toBe(true));
    expect(await bodyOf(calls, "POST", "/api/v1/ai/servers/s1/use")).toEqual({
      llm_model: "llama3.2:3b",
      use_live_stt: false,
      voice: null,
      fallback_llm_connection_id: "c-openai",
      fallback_llm_model: null,
      fallback_after_ms: 2500,
    });
  });

  it("checks a server now and replaces its token", async () => {
    let patches = 0;
    const calls = route({
      "GET /api/v1/ai/servers": () => json(200, [SERVER]),
      "POST /api/v1/ai/servers/s1/check": () => json(200, { ...SERVER, status: "ok" }),
      "PATCH /api/v1/ai/servers/s1": () =>
        ++patches === 1
          ? json(422, { error: { code: "validation_failed", message: "The server refused this token.", details: { field: "token" } } })
          : json(200, SERVER),
    });
    renderWithProviders(<AiServersCard canEdit />);
    await userEvent.click(await screen.findByRole("button", { name: /Check now/ }));
    await waitFor(() => expect(calls.filter((r) => r.method === "POST")).toHaveLength(1));

    await userEvent.click(screen.getByRole("button", { name: "More actions for GPU box" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Replace token" }));
    const dialog = within(await screen.findByRole("dialog"));
    expect(dialog.getByLabelText(/^Live speech-to-text secret/)).toHaveAttribute("placeholder", "•••••••• stored");
    await userEvent.type(dialog.getByLabelText(/^New token/), "next-token");
    await userEvent.click(dialog.getByRole("button", { name: "Check and save" }));
    expect(await dialog.findByText("The server refused this token.")).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Check and save" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    const patch = calls.filter((r) => r.method === "PATCH");
    expect(await patch[1]!.json()).toEqual({ token: "next-token", live_stt_secret: null, deepgram_key: null });
  });

  it("explains why a server in use can't be removed", async () => {
    route({
      "GET /api/v1/ai/servers": () => json(200, [SERVER]),
      "DELETE /api/v1/ai/servers/s1": () =>
        json(409, {
          error: {
            code: "conflict",
            message: "This AI server is in use by agent Hina. Choose other models there first, then remove the server.",
            details: { in_use: ["agent Hina"] },
          },
        }),
    });
    renderWithProviders(<AiServersCard canEdit />);
    await userEvent.click(await screen.findByRole("button", { name: "More actions for GPU box" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Remove" }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Remove server" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("This AI server is in use by agent Hina.");
  });

  it("sits on the AI providers screen, which shows the language model's fallback", async () => {
    route({
      "GET /api/v1/ai/servers": () => json(200, [SERVER]),
      "GET /api/v1/ai/catalog": () => json(200, CATALOG),
      "GET /api/v1/ai/connections": () => json(200, CONNECTIONS),
      "GET /api/v1/ai/models": () =>
        json(200, {
          ...MODELS,
          llm: { connection_id: "c-llm", settings: { model: "qwen2.5:1.5b" } },
          llm_fallback: { connection_id: "c-openai", settings: { model: "gpt-4.1-mini" }, after_ms: 2500, on_error: true },
        }),
      "GET /api/v1/ai-settings/latency-guide": () => json(200, { providers: [], presets: [], tts_cache_providers: [], disclaimer: "" }),
      "GET /api/v1/ai-settings/pipeline-health": () => json(200, { available: false, services: [], response: null, note: "Not set up." }),
    });
    renderWithProviders(<AiProvidersPage />);
    expect(await screen.findByText("GPU box")).toBeInTheDocument();
    const note = await screen.findByText(/^Fallback:/, { selector: "span" });
    expect(note.parentElement).toHaveTextContent("Fallback: if this model gives no output within 2.5 s or fails, OpenAI (gpt-4.1-mini) answers instead.");
  });
});
