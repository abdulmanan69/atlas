import { zodResolver } from "@hookform/resolvers/zod";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useForm, type UseFormReturn } from "react-hook-form";
import { MemoryRouter } from "react-router";

import { agentFormSchema, toForm, type AgentForm } from "@/features/voice/agent-form";
import { CallSettingsTab } from "@/features/voice/flow/call-settings-tab";
import type { AgentConfig } from "@/features/voice/queries";
import { session } from "@/lib/session";

const AGENT = "8d9c6b1e-2b55-4c1a-9a51-0e3f7f4b2a10";
const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const config = {
  company_name: "Acme",
  greeting: "Hi {{first_name}}",
  instructions: "Qualify the lead politely and briefly.",
  closing: "Thank them.",
  end_condition: "Done.",
} as AgentConfig;

const embed = {
  enabled: false,
  allowed_domains: [],
  key: null,
  script_url: "http://localhost:5173/api/v1/public/embed/widget.js",
  snippet: null,
  sessions_started: 0,
  last_used_at: null,
  rotated_at: null,
  agent_published: true,
};

let fetchMock: ReturnType<typeof vi.fn>;
let formRef: UseFormReturn<AgentForm> | null = null;

function Harness() {
  const form = useForm<AgentForm>({ resolver: zodResolver(agentFormSchema), defaultValues: toForm(config), mode: "onChange" });
  formRef = form;
  return <CallSettingsTab form={form} agentId={AGENT} canEdit />;
}

function renderTab() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <MemoryRouter>
        <Harness />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
    memberships: [{ id: "m1", role: "manager", status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
  fetchMock = vi.fn(async (req: Request) => {
    const path = new URL(req.url).pathname;
    if (path.endsWith("/web-embed") && req.method === "GET") return json(200, embed);
    if (path.endsWith("/web-embed") && req.method === "PUT") {
      const body = (await req.json()) as { enabled: boolean; allowed_domains: string[] };
      return json(200, { ...embed, ...body, key: "wk_test", snippet: '<script src="x" data-agent-key="wk_test" async></script>' });
    }
    if (path.endsWith("/audio")) return json(200, []);
    if (path.endsWith("/connections")) return json(200, []);
    if (path.endsWith("/catalog")) return json(200, { llm: [], stt: [], tts: [], embeddings: [] });
    return json(404, { error: { code: "not_found", message: "Not found" } });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
  formRef = null;
});

describe("CallSettingsTab", () => {
  it("groups every setting into sections with a navigation list", async () => {
    renderTab();
    const nav = screen.getByRole("navigation", { name: "Call settings sections" });
    const items = within(nav).getAllByRole("button").map((b) => b.textContent);
    expect(items).toEqual(["Response speed", "Length & turn-taking", "Words to recognise", "Background noise", "Voicemail & screening", "Variables", "Add to website", "Agent ID"]);
    await userEvent.click(within(nav).getByRole("button", { name: "Voicemail & screening" }));
    expect(within(nav).getByRole("button", { name: "Voicemail & screening" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("region", { name: "Agent ID" })).toHaveTextContent(AGENT);
  });

  it("sets background noise with the switch, quick picks and the default sound", async () => {
    renderTab();
    const region = screen.getByRole("region", { name: "Background noise" });
    expect(within(region).getByText("Off: calls are silent between replies.")).toBeInTheDocument();
    await userEvent.click(within(region).getByRole("switch", { name: "Background noise" }));
    expect(within(region).getByRole("slider", { name: "Background noise volume" })).toHaveAttribute("aria-valuenow", "30");
    await userEvent.click(within(region).getByRole("button", { name: /Noticeable/ }));
    expect(formRef?.getValues("call_settings.ambient.volume")).toBe(0.5);
    expect(within(region).getByRole("button", { name: /Noticeable/ })).toHaveAttribute("aria-pressed", "true");
    expect(within(region).getByText(/Using the default office ambience/)).toBeInTheDocument();
  });

  it("refuses a sound file of the wrong type before uploading", async () => {
    const { container } = renderTab();
    await userEvent.click(within(screen.getByRole("region", { name: "Background noise" })).getByRole("switch", { name: "Background noise" }));
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    expect(input).not.toBeNull();
    await userEvent.upload(input!, new File(["x"], "clip.ogg", { type: "audio/ogg" }), { applyAccept: false });
    expect(await screen.findByRole("alert")).toHaveTextContent("Choose a WAV or MP3 file.");
    expect(fetchMock.mock.calls.some(([r]) => (r as Request).method === "POST")).toBe(false);
  });

  it("asks for a recording when the voicemail message is a recording", async () => {
    renderTab();
    const region = screen.getByRole("region", { name: "Voicemail & screening" });
    await userEvent.click(within(region).getByRole("radio", { name: "Leave a message" }));
    await userEvent.click(within(region).getByRole("radiogroup", { name: "Voicemail message as" }).querySelector('[role="radio"]:last-child')!);
    expect(within(region).getByRole("button", { name: /Upload recording/ })).toBeInTheDocument();
    // Saving validates the whole form; the missing recording is reported on the field.
    await act(async () => {
      await formRef?.trigger();
    });
    expect(await within(region).findByText("Upload the recording to leave")).toBeInTheDocument();
    await userEvent.click(within(region).getByRole("radio", { name: "A separate model" }));
    expect(within(region).getByText("No eligible connection")).toBeInTheDocument();
  });

  it("validates agent variable names as they are typed", async () => {
    renderTab();
    const region = screen.getByRole("region", { name: "Variables" });
    await userEvent.click(within(region).getByRole("button", { name: /Add variable/ }));
    await userEvent.type(within(region).getByLabelText("Variable 1 name"), "first_name");
    await userEvent.type(within(region).getByLabelText("Variable 1 value"), "Sam");
    expect(await within(region).findByText("Filled from the lead; choose another name")).toBeInTheDocument();
    await userEvent.clear(within(region).getByLabelText("Variable 1 name"));
    await userEvent.type(within(region).getByLabelText("Variable 1 name"), "office_name");
    await waitFor(() => expect(within(region).queryByText(/choose another name/)).not.toBeInTheDocument());
    expect(within(region).getByText("{{office_name}}")).toBeInTheDocument();
  });

  it("turns the website widget on only with an allowed site, then shows the snippet", async () => {
    renderTab();
    const region = screen.getByRole("region", { name: "Add to website" });
    await userEvent.click(await within(region).findByRole("switch", { name: "Website widget" }));
    expect(within(region).getByRole("button", { name: /Save widget/ })).toBeDisabled();
    expect(within(region).getByText("Add at least one site to turn the widget on.")).toBeInTheDocument();
    await userEvent.type(within(region).getByLabelText("Allowed sites"), "https://Shop.Example.com/pricing{Enter}");
    expect(within(region).getByRole("list", { name: "Allowed sites" })).toHaveTextContent("shop.example.com");
    await userEvent.click(within(region).getByRole("button", { name: /Save widget/ }));
    await waitFor(() => {
      const put = fetchMock.mock.calls.map(([r]) => r as Request).find((r) => r.method === "PUT");
      expect(put && new URL(put.url).pathname).toBe(`/api/v1/agents/${AGENT}/web-embed`);
    });
  });
});
