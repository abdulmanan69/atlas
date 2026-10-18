/**
 * Typed API client generated from the backend OpenAPI schema (`pnpm gen:api`).
 *
 * - Attaches the in-memory access token and the selected organization.
 * - On 401, performs one single-flight refresh using the HttpOnly cookie, then retries.
 * - `unwrap` turns error envelopes into `ApiError` so callers never inspect raw responses.
 */
import createClient from "openapi-fetch";

import type { components, paths } from "@/types/api";
import { session } from "@/lib/session";

export type Schemas = components["schemas"];

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string | null;
  readonly details: unknown;

  constructor(status: number, code: string, message: string, requestId: string | null, details: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.requestId = requestId;
    this.details = details;
  }

  /** Field-level messages from a 422 validation error, keyed by the last path segment. */
  fieldErrors(): Record<string, string> {
    if (!Array.isArray(this.details)) return {};
    const out: Record<string, string> = {};
    for (const d of this.details as { loc?: unknown[]; msg?: string }[]) {
      const key = d.loc?.[d.loc.length - 1];
      if (typeof key === "string" && d.msg) out[key] = d.msg;
    }
    return out;
  }
}

const AUTH_PATHS = ["/api/v1/auth/login", "/api/v1/auth/signup", "/api/v1/auth/refresh"];
export const CSRF_HEADERS = { "X-Requested-With": "atlas-web" } as const;

let refreshing: Promise<boolean> | null = null;

/** Exchange the refresh cookie for a new access token. Concurrent callers share one request. */
export function refreshSession(): Promise<boolean> {
  refreshing ??= (async () => {
    try {
      const res = await fetch("/api/v1/auth/refresh", {
        method: "POST",
        credentials: "same-origin",
        headers: CSRF_HEADERS,
      });
      if (!res.ok) return false;
      session.getState().signIn((await res.json()) as Schemas["TokenResponse"]);
      return true;
    } catch {
      return false;
    } finally {
      refreshing = null;
    }
  })();
  return refreshing;
}

function withAuth(request: Request): Request {
  const { accessToken, orgId } = session.getState();
  if (accessToken) request.headers.set("Authorization", `Bearer ${accessToken}`);
  if (orgId && !request.headers.has("X-Organization-Id")) request.headers.set("X-Organization-Id", orgId);
  return request;
}

async function authFetch(input: Request): Promise<Response> {
  const retry = input.clone();
  const response = await fetch(withAuth(input));
  const isAuthCall = AUTH_PATHS.some((p) => new URL(input.url).pathname === p);
  if (response.status !== 401 || isAuthCall) return response;
  if (await refreshSession()) return fetch(withAuth(retry));
  session.getState().signOut();
  return response;
}

export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: "same-origin",
  fetch: authFetch,
});

type ErrorBody = { error?: { code?: string; message?: string; request_id?: string | null; details?: unknown } };

/** Return `data` or throw a typed ApiError. */
export async function unwrap<T>(promise: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  let result: { data?: T; error?: unknown; response: Response };
  try {
    result = await promise;
  } catch {
    throw new ApiError(0, "network_error", "Can't reach the server. Check your connection and try again.", null, null);
  }
  const { data, error, response } = result;
  if (response.ok) return data as T;
  const body = (error ?? {}) as ErrorBody;
  throw new ApiError(
    response.status,
    body.error?.code ?? "http_error",
    body.error?.message ?? `Request failed (${response.status})`,
    body.error?.request_id ?? response.headers.get("x-request-id"),
    body.error?.details,
  );
}

export function errorMessage(err: unknown): string {
  if (err instanceof ApiError) return err.message;
  return "Something went wrong. Try again.";
}
