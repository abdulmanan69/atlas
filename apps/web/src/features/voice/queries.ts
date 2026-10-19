import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type Catalog = Schemas["ProviderCatalog"];
export type ProviderOption = Schemas["ProviderOption"];
export type Connection = Schemas["ConnectionOut"];
export type ServiceChoice = Schemas["ServiceChoice"];
export type ModelSelection = Schemas["ModelSelection"];
export type AgentDetail = Schemas["AgentDetail"];
export type AgentSummary = Schemas["AgentOut"];
export type AgentConfig = Schemas["AgentConfig"];
export type ServiceKind = "llm" | "stt" | "tts" | "embeddings";

export const vk = {
  catalog: (o: string | null) => ["org", o, "ai", "catalog"] as const,
  connections: (o: string | null) => ["org", o, "ai", "connections"] as const,
  models: (o: string | null) => ["org", o, "ai", "models"] as const,
  voices: (o: string | null, provider: string, connection: string, q: string) => ["org", o, "ai", "voices", provider, connection, q] as const,
  agents: (o: string | null, archived: boolean) => ["org", o, "agents", archived] as const,
  agent: (o: string | null, id: string) => ["org", o, "agent", id] as const,
  versions: (o: string | null, id: string) => ["org", o, "agent", id, "versions"] as const,
  templates: (o: string | null) => ["org", o, "agent-templates"] as const,
  latencyGuide: (o: string | null) => ["org", o, "ai", "latency-guide"] as const,
  pipelineHealth: (o: string | null) => ["org", o, "ai", "pipeline-health"] as const,
};

export function useCatalog() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: vk.catalog(o),
    queryFn: () => unwrap(api.GET("/api/v1/ai/catalog")),
    staleTime: 10 * 60_000,
    enabled: !!o && can,
  });
}

export function useConnections() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({ queryKey: vk.connections(o), queryFn: () => unwrap(api.GET("/api/v1/ai/connections")), enabled: !!o && can });
}

export function useModelSelection() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({ queryKey: vk.models(o), queryFn: () => unwrap(api.GET("/api/v1/ai/models")), enabled: !!o && can });
}

/** Typical provider latency, fast presets and which voices can reuse audio. Static per deployment. */
export function useLatencyGuide() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({ queryKey: vk.latencyGuide(o), queryFn: () => unwrap(api.GET("/api/v1/ai-settings/latency-guide")), staleTime: 30 * 60_000, enabled: !!o && can });
}

/** Owner/admin only: the voice runtime's own latency histograms, platform-wide. */
export function usePipelineHealth() {
  const o = useOrgId();
  const can = useHasRole("admin");
  return useQuery({ queryKey: vk.pipelineHealth(o), queryFn: () => unwrap(api.GET("/api/v1/ai-settings/pipeline-health")), staleTime: 60_000, enabled: !!o && can });
}

/** Connections whose provider offers the given service. */
export function connectionsFor(kind: ServiceKind, catalog: Catalog | undefined, connections: Connection[] | undefined): Connection[] {
  if (!catalog || !connections) return [];
  const ids = new Set(catalog[kind].map((p) => p.id));
  return connections.filter((c) => ids.has(c.provider) || (c.hosted_trial && kind !== "embeddings"));
}

export function providerOf(kind: ServiceKind, catalog: Catalog | undefined, connection: Connection | undefined): ProviderOption | undefined {
  if (!catalog || !connection) return undefined;
  return catalog[kind].find((p) => p.id === connection.provider);
}

export const SERVICE_LABELS: Record<ServiceKind, { title: string; description: string }> = {
  stt: { title: "Speech-to-text", description: "Turns the caller's voice into text" },
  llm: { title: "Language model", description: "Decides what the agent says next" },
  tts: { title: "Voice", description: "Speaks the agent's replies" },
  embeddings: { title: "Knowledge search", description: "Optional: lets agents answer from uploaded documents" },
};

export const STATUS_BADGE: Record<AgentSummary["status"], { tone: "neutral" | "success" | "warn" | "accent"; label: string }> = {
  draft: { tone: "neutral", label: "Draft" },
  active: { tone: "success", label: "Active" },
  paused: { tone: "warn", label: "Paused" },
  archived: { tone: "neutral", label: "Archived" },
};
