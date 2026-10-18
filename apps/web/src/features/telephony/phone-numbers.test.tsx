import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import PhoneNumbersPage from "@/features/telephony/phone-numbers-page";
import { ProviderDialog } from "@/features/telephony/provider-dialog";
import type { Provider, ProviderOption } from "@/features/telephony/queries";
import { session } from "@/lib/session";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

const catalog: ProviderOption[] = [
  {
    id: "twilio",
    title: "Twilio",
    connectivity: "api",
    supports_outbound: true,
    fields: [
      { name: "account_sid", label: "Account SID", secret: true, required: true, help: "Starts with AC; Twilio Console → Account Info", multiline: false },
      { name: "auth_token", label: "Auth Token", secret: true, required: true, help: null, multiline: false },
    ],
  },
  {
    id: "ari",
    title: "SIP trunk (Asterisk ARI)",
    connectivity: "sip",
    supports_outbound: true,
    fields: [
      { name: "ari_endpoint", label: "ARI endpoint", secret: false, required: true, help: "Base URL of the Asterisk REST Interface", multiline: false },
      { name: "app_password", label: "ARI password", secret: true, required: true, help: null, multiline: false },
    ],
  },
];

const twilio: Provider = {
  id: "7",
  name: "Main Twilio",
  provider: "twilio",
  provider_title: "Twilio",
  connectivity: "api",
  configured_fields: ["account_sid", "auth_token"],
  is_default_outbound: true,
  inactive: false,
  inactive_reason: null,
  number_count: 2,
  supports_outbound: true,
  ready_for_outbound: true,
  outbound_blocked_reason: null,
  supports_number_sync: true,
};

type Route = (req: Request) => Response | Promise<Response>;
function mockApi(routes: Record<string, Route>) {
  const calls: { method: string; path: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: Request | string) => {
    const req = input instanceof Request ? input : new Request(input);
    const path = new URL(req.url).pathname;
    const text = await req.clone().text();
    calls.push({ method: req.method, path, body: text ? JSON.parse(text) : null });
    const route = routes[`${req.method} ${path}`];
    return route ? route(req) : json(404, { error: { code: "not_found", message: `No mock for ${req.method} ${path}` } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return calls;
}

function renderWithProviders(ui: React.ReactElement) {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <MemoryRouter>{ui}</MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
    memberships: [
      {
        id: "m1",
        role: "owner",
        status: "active",
        organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" },
      },
    ],
  } as never);
});
afterEach(() => vi.unstubAllGlobals());

describe("PhoneNumbersPage", () => {
  it("explains what to do when no provider is connected", async () => {
    mockApi({
      "GET /api/v1/telephony/providers/catalog": () => json(200, catalog),
      "GET /api/v1/telephony/providers": () => json(200, []),
      "GET /api/v1/telephony/numbers": () => json(200, []),
      "GET /api/v1/agents": () => json(200, []),
    });
    renderWithProviders(<PhoneNumbersPage />);
    expect(await screen.findByText("Connect a provider to make and receive calls.")).toBeInTheDocument();
    // Offered twice on purpose: in the header and inside the empty state.
    expect(screen.getAllByRole("button", { name: "Connect provider" })).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /Add number/ })).not.toBeInTheDocument();
  });

  it("shows the API's conflict message when a provider in use can't be disconnected", async () => {
    mockApi({
      "GET /api/v1/telephony/providers/catalog": () => json(200, catalog),
      "GET /api/v1/telephony/providers": () => json(200, [twilio]),
      "GET /api/v1/telephony/numbers": () => json(200, []),
      "GET /api/v1/agents": () => json(200, []),
      "DELETE /api/v1/telephony/providers/7": () => json(409, { error: { code: "conflict", message: "This provider is still used by 2 calls in progress" } }),
    });
    renderWithProviders(<PhoneNumbersPage />);
    expect(await screen.findByText("Main Twilio")).toBeInTheDocument();
    expect(screen.getByText("Default for outbound")).toBeInTheDocument();
    expect(screen.getByText("Account SID stored")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Actions for Main Twilio" }));
    await userEvent.click(await screen.findByRole("menuitem", { name: "Disconnect" }));
    const dialog = await screen.findByRole("alertdialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Disconnect provider" }));
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("This provider is still used by 2 calls in progress");
  });
});

describe("ProviderDialog", () => {
  it("renders the catalog's credential fields for the chosen provider and sends them write-only", async () => {
    const calls = mockApi({
      "POST /api/v1/telephony/providers": () => json(201, { ...twilio, name: "Sales account" }),
    });
    renderWithProviders(<ProviderDialog open onOpenChange={() => undefined} catalog={catalog} />);

    const provider = screen.getByRole("combobox", { name: "Provider" });
    provider.focus();
    await userEvent.keyboard("{Enter}");
    await screen.findByRole("listbox");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(provider).toHaveTextContent("Twilio"));

    const sid = screen.getByLabelText(/Account SID/);
    const token = screen.getByLabelText(/Auth Token/);
    expect(sid).toHaveAttribute("type", "password");
    expect(token).toHaveAttribute("type", "password");
    expect(sid).toHaveAttribute("aria-required", "true");
    expect(screen.getByText("Starts with AC; Twilio Console → Account Info")).toBeInTheDocument();
    expect(screen.queryByLabelText(/ARI endpoint/)).not.toBeInTheDocument();

    const connect = screen.getByRole("button", { name: "Connect" });
    expect(connect).toBeDisabled();
    await userEvent.clear(screen.getByLabelText(/^Name/));
    await userEvent.type(screen.getByLabelText(/^Name/), "Sales account");
    await userEvent.type(sid, "ACxxxx");
    await userEvent.type(token, "secret");
    expect(connect).toBeEnabled();
    await userEvent.click(connect);

    await waitFor(() => expect(calls.some((c) => c.method === "POST")).toBe(true));
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ name: "Sales account", provider: "twilio", credentials: { account_sid: "ACxxxx", auth_token: "secret" }, is_default_outbound: false });
  });

  it("when editing, shows stored placeholders and never asks for a provider again", () => {
    mockApi({});
    renderWithProviders(<ProviderDialog open onOpenChange={() => undefined} catalog={catalog} existing={twilio} />);
    expect(screen.queryByRole("combobox", { name: "Provider" })).not.toBeInTheDocument();
    expect(screen.getByLabelText(/Account SID/)).toHaveAttribute("placeholder", "•••••••• stored");
    expect(screen.getByLabelText(/Account SID/)).not.toHaveAttribute("aria-required");
    expect(screen.getByRole("button", { name: "Save changes" })).toBeEnabled();
  });
});
