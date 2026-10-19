import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";

import SignInPage from "@/features/auth/sign-in-page";
import { session } from "@/lib/session";

// Declarative router: data routers build fetch Requests whose jsdom AbortSignal
// Node's undici rejects. The page uses no data-router APIs.
function renderPage() {
  render(
    <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
      <MemoryRouter initialEntries={["/sign-in?next=%2Fleads"]}>
        <Routes>
          <Route path="/sign-in" element={<SignInPage />} />
          <Route path="/" element={<p>Home</p>} />
          <Route path="/leads" element={<p>Leads page</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

/** Answers the sign-in options query; `login` answers everything else. */
function stubFetch(login: () => Response, signupEnabled = true) {
  const fetchMock = vi.fn(async (input: Request | string) => {
    const url = typeof input === "string" ? input : input.url;
    return url.endsWith("/api/v1/auth/options") ? json({ signup_enabled: signupEnabled }) : login();
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const loginCalls = (m: ReturnType<typeof stubFetch>) =>
  m.mock.calls.filter(([input]) => (typeof input === "string" ? input : input.url).endsWith("/auth/login"));

describe("SignInPage", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("offers sign-up only when the deployment allows it", async () => {
    stubFetch(() => json({}), true);
    renderPage();
    expect(await screen.findByRole("link", { name: "Create an organization" })).toBeInTheDocument();
  });

  it("hides sign-up when the deployment has it closed", async () => {
    const m = stubFetch(() => json({}), false);
    renderPage();
    await vi.waitFor(() => expect(m).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(screen.queryByRole("link", { name: "Create an organization" })).not.toBeInTheDocument();
  });

  it("validates before calling the API", async () => {
    const fetchMock = stubFetch(() => json({}));
    renderPage();
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Enter a valid email address")).toBeInTheDocument();
    expect(screen.getByText("Enter your password")).toBeInTheDocument();
    expect(loginCalls(fetchMock)).toHaveLength(0);
  });

  it("shows the server's message when sign-in fails", async () => {
    stubFetch(() => json({ error: { code: "unauthenticated", message: "Invalid email or password" } }, 401));
    renderPage();
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "wrong-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Invalid email or password");
  });

  it("signs in and continues to the requested page", async () => {
    stubFetch(() =>
      json({
        access_token: "t",
        token_type: "bearer",
        expires_in: 900,
        user: { id: "u1", email: "a@example.com", full_name: "A", created_at: "2026-01-01T00:00:00Z" },
        memberships: [],
      }),
    );
    renderPage();
    await userEvent.type(screen.getByLabelText("Email"), "a@example.com");
    await userEvent.type(screen.getByLabelText("Password"), "correct-password");
    await userEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("Leads page")).toBeInTheDocument();
    expect(session.getState().status).toBe("authenticated");
  });
});
