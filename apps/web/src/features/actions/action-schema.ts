/**
 * Action editor model. Pure: the derivation rules mirror the API so the editor can
 * preview the function name and refuse what the server would refuse, before a request.
 */
import { z } from "zod";

import type { Schemas } from "@/lib/api";

export type Action = Schemas["ActionOut"];
export type ActionCreate = Schemas["ActionCreate"];
export type ActionUpdate = Schemas["ActionUpdate"];
export type ActionAuthType = Schemas["ActionAuth"]["type"];

export const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;
export const PARAM_TYPES = ["string", "number", "boolean", "object", "array"] as const;
export const AUTH_TYPES: { value: ActionAuthType; label: string; description: string }[] = [
  { value: "bearer", label: "Bearer token", description: "Authorization: Bearer <token>" },
  { value: "api_key", label: "API key header", description: "A header of your choice carrying the key" },
  { value: "basic", label: "Basic", description: "Username and password" },
  { value: "custom_header", label: "Custom header", description: "Any header name and value" },
];
export const AUTH_LABEL: Record<ActionAuthType, string> = Object.fromEntries(AUTH_TYPES.map((a) => [a.value, a.label])) as Record<ActionAuthType, string>;

/** Headers the API refuses: set by the credential or by the HTTP client itself. */
export const RESERVED_HEADERS = new Set(["authorization", "proxy-authorization", "cookie", "host", "content-length", "transfer-encoding", "connection"]);
export const MAX_HEADERS = 20;
export const MAX_PARAMS = 30;

/** Same rule as the API: lower-case, runs of anything but a-z 0-9 _ become one `_`, trimmed. */
export function deriveFunctionName(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9_]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/** Client-side half of the outbound destination policy; the server re-checks DNS on every save and test. */
export function urlProblem(raw: string): string | null {
  const url = raw.trim();
  if (!url) return "URL is required";
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return "Enter a full URL, such as https://api.example.com/bookings";
  }
  if (parsed.protocol !== "https:") return "The URL must use https://";
  if (parsed.username || parsed.password) return "Remove the username and password from the URL; use the authentication section instead";
  if (!parsed.hostname) return "The URL needs a host";
  return null;
}

const identifier = /^[A-Za-z_][A-Za-z0-9_]*$/;
const nameField = z.string().trim().min(1, "Name is required").max(64, "At most 64 characters").regex(identifier, "Letters, digits and underscores only; can't start with a digit");

const headerRow = z.object({ name: z.string().trim().max(100), value: z.string().max(4000) });
const paramRow = z.object({
  name: nameField,
  type: z.enum(PARAM_TYPES),
  description: z.string().trim().min(1, "Tell the model what to provide").max(500, "At most 500 characters"),
  required: z.boolean(),
});
const presetRow = z.object({
  name: nameField,
  type: z.enum(PARAM_TYPES),
  value: z.string().trim().min(1, "Enter a value or a {{template}}").max(4000),
  required: z.boolean(),
});
const authFields = z.object({
  type: z.enum(["bearer", "api_key", "basic", "custom_header"]),
  header_name: z.string().trim().max(100),
  token: z.string().max(4000),
  api_key: z.string().max(4000),
  username: z.string().max(1000),
  password: z.string().max(4000),
  header_value: z.string().max(4000),
});

export const actionSchema = z
  .object({
    name: z.string().trim().min(1, "Name is required").max(80, "At most 80 characters"),
    description: z.string().trim().min(1, "Describe when the model should use this and what it gets back").max(2000, "At most 2000 characters"),
    method: z.enum(METHODS),
    url: z.string().trim().max(2048, "At most 2048 characters"),
    headers: z.array(headerRow).max(MAX_HEADERS, `At most ${MAX_HEADERS} headers`),
    parameters: z.array(paramRow).max(MAX_PARAMS, `At most ${MAX_PARAMS} parameters`),
    preset_parameters: z.array(presetRow).max(MAX_PARAMS, `At most ${MAX_PARAMS} preset values`),
    timeout_ms: z.number({ error: "Enter a timeout in milliseconds" }).int("Whole milliseconds").min(100, "At least 100 ms").max(15000, "At most 15000 ms"),
    body_format: z.enum(["json", "form"]),
    say_while_running: z.string().trim().max(300, "At most 300 characters"),
    /** none: no credential · keep: leave the stored one (edit only) · set: send `auth` */
    auth_mode: z.enum(["none", "keep", "set"]),
    auth: authFields,
  })
  .superRefine((v, ctx) => {
    const issue = (path: (string | number)[], message: string) => ctx.addIssue({ code: "custom", path, message });
    if (deriveFunctionName(v.name) === "") issue(["name"], "The name needs at least one letter or digit");
    const url = urlProblem(v.url);
    if (url) issue(["url"], url);

    const credentialHeader = v.auth_mode === "set" ? (v.auth.type === "bearer" || v.auth.type === "basic" ? "authorization" : v.auth.header_name.trim().toLowerCase()) : null;
    const seenHeaders = new Set<string>();
    v.headers.forEach((h, i) => {
      const key = h.name.toLowerCase();
      if (!h.name && !h.value) return;
      if (!h.name) return issue(["headers", i, "name"], "Header name is required");
      if (RESERVED_HEADERS.has(key)) return issue(["headers", i, "name"], "Authentication and framing headers can't be set here; use the authentication section");
      if (credentialHeader && key === credentialHeader) return issue(["headers", i, "name"], "This header is already set by the credential");
      if (seenHeaders.has(key)) return issue(["headers", i, "name"], "Duplicate header");
      seenHeaders.add(key);
    });

    const seen = new Map<string, string>();
    v.parameters.forEach((p, i) => {
      if (!p.name) return;
      if (seen.has(p.name)) issue(["parameters", i, "name"], "Names must be unique across parameters and preset values");
      seen.set(p.name, "parameter");
    });
    v.preset_parameters.forEach((p, i) => {
      if (!p.name) return;
      if (seen.has(p.name)) issue(["preset_parameters", i, "name"], "Names must be unique across parameters and preset values");
      seen.set(p.name, "preset");
    });

    if (v.auth_mode === "set") {
      const a = v.auth;
      const need = (field: keyof typeof a, message: string) => {
        if (!String(a[field]).trim()) issue(["auth", field], message);
      };
      if (a.type === "bearer") need("token", "Enter the token");
      if (a.type === "api_key") {
        need("header_name", "Enter the header name, for example X-API-Key");
        need("api_key", "Enter the key");
      }
      if (a.type === "basic") {
        need("username", "Enter the username");
        need("password", "Enter the password");
      }
      if (a.type === "custom_header") {
        need("header_name", "Enter the header name");
        need("header_value", "Enter the header value");
      }
      if ((a.type === "api_key" || a.type === "custom_header") && RESERVED_HEADERS.has(a.header_name.trim().toLowerCase()) && a.header_name.trim().toLowerCase() !== "authorization") {
        issue(["auth", "header_name"], "That header is reserved by the HTTP client");
      }
    }
  });

export type ActionFormValues = z.infer<typeof actionSchema>;

export const EMPTY_AUTH: ActionFormValues["auth"] = { type: "bearer", header_name: "", token: "", api_key: "", username: "", password: "", header_value: "" };

export function emptyActionValues(): ActionFormValues {
  return {
    name: "",
    description: "",
    method: "POST",
    url: "",
    headers: [],
    parameters: [],
    preset_parameters: [],
    timeout_ms: 5000,
    body_format: "json",
    say_while_running: "",
    auth_mode: "none",
    auth: EMPTY_AUTH,
  };
}

export function valuesFromAction(a: Action): ActionFormValues {
  return {
    name: a.name,
    description: a.description,
    method: a.method ?? "POST",
    url: a.url,
    headers: Object.entries(a.headers ?? {}).map(([name, value]) => ({ name, value })),
    parameters: (a.parameters ?? []).map((p) => ({ name: p.name, type: p.type ?? "string", description: p.description, required: p.required ?? true })),
    preset_parameters: (a.preset_parameters ?? []).map((p) => ({ name: p.name, type: p.type ?? "string", value: p.value, required: p.required ?? true })),
    timeout_ms: a.timeout_ms ?? 5000,
    body_format: a.body_format ?? "json",
    say_while_running: a.say_while_running ?? "",
    auth_mode: a.auth ? "keep" : "none",
    auth: { ...EMPTY_AUTH, type: a.auth?.type ?? "bearer", header_name: a.auth?.header_name ?? "" },
  };
}

function authBody(v: ActionFormValues): Schemas["ActionAuth"] {
  const a = v.auth;
  switch (a.type) {
    case "bearer":
      return { type: "bearer", token: a.token };
    case "api_key":
      return { type: "api_key", header_name: a.header_name.trim(), api_key: a.api_key };
    case "basic":
      return { type: "basic", username: a.username, password: a.password };
    case "custom_header":
      return { type: "custom_header", header_name: a.header_name.trim(), header_value: a.header_value };
  }
}

function common(v: ActionFormValues) {
  return {
    name: v.name,
    description: v.description,
    method: v.method,
    url: v.url.trim(),
    headers: Object.fromEntries(v.headers.filter((h) => h.name.trim()).map((h) => [h.name.trim(), h.value])),
    parameters: v.parameters.map((p) => ({ name: p.name, type: p.type, description: p.description, required: p.required })),
    preset_parameters: v.preset_parameters.map((p) => ({ name: p.name, type: p.type, value: p.value, required: p.required })),
    timeout_ms: v.timeout_ms,
    body_format: v.body_format,
    say_while_running: v.say_while_running || null,
  };
}

export function toCreateBody(v: ActionFormValues): ActionCreate {
  return { ...common(v), auth: v.auth_mode === "set" ? authBody(v) : null };
}

/** `auth` omitted keeps the stored credential; `null` removes it. */
export function toUpdateBody(v: ActionFormValues, existing: Action): ActionUpdate {
  const body: ActionUpdate = common(v);
  if (v.auth_mode === "set") body.auth = authBody(v);
  else if (v.auth_mode === "none" && existing.auth) body.auth = null;
  return body;
}

/** Host and path for the list; the query string and scheme are noise there. */
export function urlSummary(url: string): { host: string; path: string } {
  try {
    const u = new URL(url);
    return { host: u.host, path: u.pathname === "/" ? "" : u.pathname };
  } catch {
    return { host: url, path: "" };
  }
}
