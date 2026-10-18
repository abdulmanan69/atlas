import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { ApiKeysTab } from "@/features/developer/api-keys-tab";
import { keyState, type ApiKey } from "@/features/developer/queries";
import { WebhooksTab } from "@/features/developer/webhooks-tab";
import { session } from "@/lib/session";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

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

const key: ApiKey = {
  id: "k1",
  name: "Zapier sync",
  key_prefix: "atlas_k3Xw",
  role: "manager",
  created_by_user_id: "u1",
  created_at: "2026-10-01T00:00:00Z",
  last_used_at: null,
  expires_at: null,
  revoked_at: null,
};

beforeEach(() => {
  session.getState().signIn({
    access_token: "t",
    token_type: "bearer",
    expires_in: 900,
    user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
    memberships: [{ id: "m1", role: "admin", status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
  } as never);
});
afterEach(() => vi.unstubAllGlobals());

describe("keyState", () => {
  it("distinguishes active, expired and revoked keys", () => {
    expect(keyState(key)).toBe("active");
    expect(keyState({ ...key, expires_at: "2020-01-01T00:00:00Z" })).toBe("expired");
    expect(keyState({ ...key, revoked_at: "2026-10-02T00:00:00Z", expires_at: "2020-01-01T00:00:00Z" })).toBe("revoked");
  });
});

describe("ApiKeysTab", () => {
  it("shows the full key once after creating it", async () => {
    const calls = mockApi({
      "GET /api/v1/developer/api-keys": () => json(200, []),
      "GET /api/v1/members": () => json(200, []),
      "POST /api/v1/developer/api-keys": () => json(201, { ...key, key: "atlas_k3XwFULLSECRET" }),
    });
    renderWithProviders(<ApiKeysTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Create key" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^Name/), "Zapier sync");
    await userEvent.click(within(dialog).getByRole("button", { name: "Create key" }));

    expect(await within(dialog).findByText("atlas_k3XwFULLSECRET")).toBeInTheDocument();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("You won't see this key again.");
    const post = calls.find((c) => c.method === "POST")!;
    expect(post.body).toEqual({ name: "Zapier sync", role: "manager", expires_at: null });
  });

  it("caps the role choices at the current member's role", async () => {
    mockApi({ "GET /api/v1/developer/api-keys": () => json(200, []), "GET /api/v1/members": () => json(200, []) });
    renderWithProviders(<ApiKeysTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Create key" }));
    const role = await screen.findByRole("combobox", { name: /Role/ });
    role.focus();
    await userEvent.keyboard("{Enter}");
    const list = await screen.findByRole("listbox");
    expect(within(list).getByRole("option", { name: /Admin/ })).toBeInTheDocument();
    expect(within(list).queryByRole("option", { name: /Owner/ })).not.toBeInTheDocument();
  });

  it("asks for confirmation before revoking", async () => {
    const calls = mockApi({
      "GET /api/v1/developer/api-keys": () => json(200, [key]),
      "GET /api/v1/members": () => json(200, []),
      "DELETE /api/v1/developer/api-keys/k1": () => new Response(null, { status: 204 }),
    });
    renderWithProviders(<ApiKeysTab />);
    expect(await screen.findByText("Zapier sync")).toBeInTheDocument();
    expect(screen.getByText("atlas_k3Xw…")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Revoke" }));
    const dialog = await screen.findByRole("alertdialog");
    expect(calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(within(dialog).getByRole("button", { name: "Revoke key" }));
    await waitFor(() => expect(calls.some((c) => c.method === "DELETE" && c.path === "/api/v1/developer/api-keys/k1")).toBe(true));
  });
});

describe("WebhooksTab", () => {
  it("builds the event checklist from the catalogue and shows the secret once on create", async () => {
    const calls = mockApi({
      "GET /api/v1/developer/webhooks": () => json(200, []),
      "GET /api/v1/developer/events": () =>
        json(200, [
          { type: "lead.created", description: "A lead was created." },
          { type: "call.completed", description: "A call ended." },
          { type: "*", description: "Every event type, including ones added later." },
        ]),
      "POST /api/v1/developer/webhooks": () =>
        json(201, {
          id: "w1",
          url: "https://hooks.example.com/atlas",
          description: null,
          events: ["lead.created"],
          is_active: true,
          failure_count: 0,
          disabled_at: null,
          disabled_reason: null,
          created_by_user_id: "u1",
          created_at: "2026-10-18T00:00:00Z",
          updated_at: "2026-10-18T00:00:00Z",
          secret: "whsec_ONLYONCE",
        }),
    });
    renderWithProviders(<WebhooksTab />);
    await userEvent.click(await screen.findByRole("button", { name: "Add endpoint" }));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText(/^URL/), "https://hooks.example.com/atlas");
    await userEvent.click(within(dialog).getByRole("button", { name: "Register endpoint" }));
    expect(await within(dialog).findByText("Choose at least one event, or all events")).toBeInTheDocument();
    expect(calls.some((c) => c.method === "POST")).toBe(false);

    await userEvent.click(await within(dialog).findByRole("switch", { name: "lead.created" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Register endpoint" }));
    expect(await within(dialog).findByText("whsec_ONLYONCE")).toBeInTheDocument();
    expect(within(dialog).getByRole("alert")).toHaveTextContent("You won't see this secret again.");
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ url: "https://hooks.example.com/atlas", description: null, events: ["lead.created"] });
  });
});
