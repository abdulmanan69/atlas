import { api, ApiError, unwrap } from "@/lib/api";
import { session } from "@/lib/session";

const json = (status: number, body: unknown, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", ...headers } });

const tokens = (access: string) => ({
  access_token: access,
  token_type: "bearer",
  expires_in: 900,
  user: { id: "u1", email: "a@example.com", full_name: "A", created_at: "2026-01-01T00:00:00Z" },
  memberships: [
    {
      id: "m1",
      role: "owner",
      status: "active",
      organization: { id: "org-1", name: "Org", slug: "org", timezone: "UTC", default_country: "GB", created_at: "2026-01-01T00:00:00Z" },
    },
  ],
});

describe("api client", () => {
  beforeEach(() => {
    session.getState().signIn(tokens("old-token") as never);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("sends the access token and selected organization", async () => {
    const fetchMock = vi.fn(async (_req: Request) => json(200, { status: "ok" }));
    vi.stubGlobal("fetch", fetchMock);
    await unwrap(api.GET("/api/v1/organization"));
    const req = fetchMock.mock.calls[0]![0];
    expect(req.headers.get("Authorization")).toBe("Bearer old-token");
    expect(req.headers.get("X-Organization-Id")).toBe("org-1");
  });

  it("refreshes once on 401 and retries with the new token", async () => {
    const seen: string[] = [];
    const fetchMock = vi.fn(async (input: Request | string) => {
      const url = typeof input === "string" ? input : input.url;
      if (url.endsWith("/api/v1/auth/refresh")) return json(200, tokens("new-token"));
      const auth = (input as Request).headers.get("Authorization") ?? "";
      seen.push(auth);
      return auth === "Bearer new-token" ? json(200, { name: "Org" }) : json(401, { error: { code: "unauthenticated", message: "expired" } });
    });
    vi.stubGlobal("fetch", fetchMock);

    const org = await unwrap(api.GET("/api/v1/organization"));
    expect(org).toEqual({ name: "Org" });
    expect(seen).toEqual(["Bearer old-token", "Bearer new-token"]);
    expect(session.getState().accessToken).toBe("new-token");
  });

  it("signs out when the refresh fails", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(401, { error: { code: "unauthenticated", message: "nope" } })),
    );
    await expect(unwrap(api.GET("/api/v1/organization"))).rejects.toBeInstanceOf(ApiError);
    expect(session.getState().status).toBe("anonymous");
  });

  it("maps the error envelope, including field errors and request id", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        json(422, {
          error: {
            code: "validation_failed",
            message: "Request validation failed",
            request_id: "req-123",
            details: [{ loc: ["body", "email"], msg: "value is not a valid email address", type: "value_error" }],
          },
        }),
      ),
    );
    const err = await unwrap(api.POST("/api/v1/leads", { body: { first_name: "A", consent_status: "unknown" } })).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const apiErr = err as ApiError;
    expect(apiErr.code).toBe("validation_failed");
    expect(apiErr.requestId).toBe("req-123");
    expect(apiErr.fieldErrors()).toEqual({ email: "value is not a valid email address" });
  });

  it("reports network failures clearly", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );
    const err = (await unwrap(api.GET("/api/v1/organization")).catch((e: unknown) => e)) as ApiError;
    expect(err.code).toBe("network_error");
  });
});
