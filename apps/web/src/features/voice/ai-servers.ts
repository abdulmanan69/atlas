/** AI servers: self-hosted model hosts reached through their AI gateway (ADR 0007). */
import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import { api, ApiError, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type AiServer = Schemas["AiServerOut"];
export type AiServerRegistered = Schemas["AiServerRegistered"];
export type AiServerService = Schemas["AiServerService"];
export type AiServerSetup = Schemas["AiServerSetup"];

export const aiServersKey = (o: string | null) => ["org", o, "ai", "servers"] as const;

/** Managers and up. Refreshed as often as the API checks the servers (every two minutes). */
export function useAiServers() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: aiServersKey(o),
    queryFn: () => unwrap(api.GET("/api/v1/ai/servers")),
    enabled: !!o && can,
    refetchInterval: 120_000,
  });
}

export const SERVER_STATUS: Record<AiServer["status"], { label: string; dot: string }> = {
  ok: { label: "All services up", dot: "bg-success" },
  degraded: { label: "Some services down", dot: "bg-warn" },
  down: { label: "Down", dot: "bg-danger" },
  unchecked: { label: "Not checked yet", dot: "bg-faint" },
};

export const SERVICE_STATE: Record<AiServerService["state"], { label: string; dot: string }> = {
  up: { label: "Up", dot: "bg-success" },
  loading: { label: "Loading its model", dot: "bg-warn" },
  down: { label: "Down", dot: "bg-danger" },
  not_offered: { label: "Not offered", dot: "bg-line-strong" },
};

/** Choices for how long the server may stay silent before the fallback answers. */
export const FALLBACK_DELAYS_MS = [1000, 1500, 2500, 4000, 6000] as const;
export const DEFAULT_FALLBACK_MS = 2500;

export const formatMs = (ms: number): string => (ms >= 1000 ? `${ms / 1000} s` : `${ms} ms`);

export function serviceOf(server: AiServer, service: AiServerService["service"]): AiServerService | undefined {
  return server.services.find((s) => s.service === service);
}

/** What a service offers, in a few words: its models, voices or sessions. */
export function serviceSummary(s: AiServerService): string {
  if (s.state === "not_offered") return "Not offered by this server";
  const parts = [...(s.models ?? [])];
  if (s.service === "tts" && s.voices != null) parts.push(`${s.voices} voices`);
  if (s.service === "live_stt" && s.max_sessions != null) parts.push(`${s.sessions ?? 0} of ${s.max_sessions} calls`);
  return parts.join(" · ") || "No models listed";
}

/** The message of an API error about one field (`details.field`, or a request validation error). */
export function fieldError(err: unknown, field: string): string | undefined {
  if (!(err instanceof ApiError)) return undefined;
  const details = err.details as { field?: unknown } | null | undefined;
  if (details && !Array.isArray(details) && details.field === field) return err.message;
  return err.fieldErrors()[field];
}

/** The error to show above a form: none when it is already shown next to one of its fields. */
export function formError(err: unknown, fields: readonly string[]): string | null {
  if (!err) return null;
  if (fields.some((f) => fieldError(err, f))) return null;
  return errorMessage(err);
}
