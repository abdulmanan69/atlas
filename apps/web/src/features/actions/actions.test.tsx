import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";

import { ActionEditorDialog } from "@/features/actions/action-editor-dialog";
import { actionSchema, deriveFunctionName, emptyActionValues, toUpdateBody, urlProblem, valuesFromAction, type Action } from "@/features/actions/action-schema";
import { session } from "@/lib/session";

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

describe("action schema", () => {
  it("derives the function name the way the API does", () => {
    expect(deriveFunctionName("Book a viewing!")).toBe("book_a_viewing");
    expect(deriveFunctionName("  Check -- Availability (v2) ")).toBe("check_availability_v2");
    expect(deriveFunctionName("!!!")).toBe("");
  });

  it("enforces the destination policy's client-side half", () => {
    expect(urlProblem("")).toBe("URL is required");
    expect(urlProblem("api.example.com/x")).toMatch(/full URL/);
    expect(urlProblem("http://api.example.com/x")).toBe("The URL must use https://");
    expect(urlProblem("https://user:pw@api.example.com/x")).toMatch(/username and password/);
    expect(urlProblem("https://api.example.com/x")).toBeNull();
  });

  it("refuses reserved headers, duplicate names and incomplete credentials", () => {
    const result = actionSchema.safeParse({
      ...emptyActionValues(),
      name: "Book",
      description: "Books it",
      url: "https://api.example.com/book",
      headers: [{ name: "Authorization", value: "x" }],
      parameters: [{ name: "date", type: "string", description: "When", required: true }],
      preset_parameters: [{ name: "date", type: "string", value: "{{initial_context.date}}", required: true }],
      auth_mode: "set",
      auth: { ...emptyActionValues().auth, type: "api_key", header_name: "", api_key: "" },
    });
    expect(result.success).toBe(false);
    const paths = result.success ? [] : result.error.issues.map((i) => i.path.join("."));
    expect(paths).toEqual(expect.arrayContaining(["headers.0.name", "preset_parameters.0.name", "auth.header_name", "auth.api_key"]));
  });

  it("keeps, replaces or removes the stored credential on update", () => {
    const existing: Action = { id: "a1", name: "Book", description: "d", url: "https://x.example", function_name: "book", auth: { type: "bearer", header_name: "Authorization", status: "configured" } };
    const values = valuesFromAction(existing);
    expect(values.auth_mode).toBe("keep");
    expect(toUpdateBody(values, existing)).not.toHaveProperty("auth");
    expect(toUpdateBody({ ...values, auth_mode: "none" }, existing).auth).toBeNull();
    expect(toUpdateBody({ ...values, auth_mode: "set", auth: { ...values.auth, type: "bearer", token: "t" } }, existing).auth).toEqual({ type: "bearer", token: "t" });
  });
});

function renderDialog() {
  return render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })}>
      <MemoryRouter>
        <ActionEditorDialog open onOpenChange={() => undefined} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ActionEditorDialog", () => {
  beforeEach(() => {
    session.getState().signIn({
      access_token: "t",
      token_type: "bearer",
      expires_in: 900,
      user: { id: "u1", email: "a@example.com", full_name: "Ada", created_at: "2026-01-01T00:00:00Z" },
      memberships: [{ id: "m1", role: "manager", status: "active", organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" } }],
    } as never);
  });
  afterEach(() => vi.unstubAllGlobals());

  // Large form: paste instead of typing keystroke by keystroke, and allow for a loaded machine.
  const fill = async (label: RegExp, text: string) => {
    const el = screen.getByLabelText(label);
    await userEvent.click(el);
    await userEvent.paste(text);
  };

  it("validates before calling the API and shows the server's policy rejection verbatim", { timeout: 20_000 }, async () => {
    const fetchMock = vi.fn(async () => json(422, { error: { code: "validation_failed", message: "Action URL must point to a public address", details: { field: "url" } } }));
    vi.stubGlobal("fetch", fetchMock);
    renderDialog();

    await userEvent.click(screen.getByRole("button", { name: "Create action" }));
    expect(await screen.findByText("Name is required")).toBeInTheDocument();
    expect(screen.getByText("Describe when the model should use this and what it gets back")).toBeInTheDocument();
    expect(screen.getByText("URL is required")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    await fill(/^Name/, "Book a viewing!");
    expect(await screen.findByText("book_a_viewing")).toBeInTheDocument();
    await fill(/^Description/, "Books a viewing once the caller confirmed a time.");
    await fill(/^URL/, "http://10.0.0.5/book");
    await userEvent.click(screen.getByRole("button", { name: "Create action" }));
    expect(await screen.findByText("The URL must use https://")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();

    await userEvent.clear(screen.getByLabelText(/^URL/));
    await fill(/^URL/, "https://internal.example/book");
    await userEvent.click(screen.getByRole("button", { name: "Create action" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    const alerts = await screen.findAllByText("Action URL must point to a public address");
    expect(alerts.length).toBeGreaterThan(0);
  });
});
