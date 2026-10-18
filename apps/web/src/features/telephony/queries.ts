/**
 * Telephony server state: providers, numbers and the per-provider setup instructions.
 * Keys sit under ["org", orgId, "telephony", …] so `useInvalidate("telephony")` refreshes all of it.
 */
import { useQuery } from "@tanstack/react-query";

import { useAgents, useOrgId } from "@/features/queries";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type ProviderOption = Schemas["TelephonyProviderOption"];
export type CredentialField = Schemas["CredentialField"];
export type Provider = Schemas["ProviderOut"];
export type PhoneNumber = Schemas["NumberOut"];
export type SetupInstructions = Schemas["SetupInstructions"];
export type NumberSyncResult = Schemas["NumberSyncResult"];

export const tk = {
  catalog: (o: string | null) => ["org", o, "telephony", "catalog"] as const,
  providers: (o: string | null) => ["org", o, "telephony", "providers"] as const,
  numbers: (o: string | null) => ["org", o, "telephony", "numbers"] as const,
  setup: (o: string | null, provider: string) => ["org", o, "telephony", "setup", provider] as const,
};

export function useTelephonyCatalog() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: tk.catalog(o),
    queryFn: () => unwrap(api.GET("/api/v1/telephony/providers/catalog")),
    staleTime: 10 * 60_000,
    enabled: !!o && can,
  });
}

export function useProviders() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({ queryKey: tk.providers(o), queryFn: () => unwrap(api.GET("/api/v1/telephony/providers")), enabled: !!o && can });
}

export function useNumbers() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({ queryKey: tk.numbers(o), queryFn: () => unwrap(api.GET("/api/v1/telephony/numbers")), enabled: !!o && can });
}

export function useSetupInstructions(provider: string | null) {
  const o = useOrgId();
  return useQuery({
    queryKey: tk.setup(o, provider ?? ""),
    queryFn: () => unwrap(api.GET("/api/v1/telephony/setup-instructions/{provider}", { params: { path: { provider: provider! } } })),
    staleTime: 5 * 60_000,
    enabled: !!o && !!provider,
  });
}

/** Agents a number can be routed to: published and not archived. */
export function usePublishedAgents() {
  const agents = useAgents(false);
  return { ...agents, data: agents.data?.filter((a) => a.published_version !== null && a.status !== "archived") };
}

const LOCAL_HOSTS = new Set(["localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "::1", "[::1]"]);

/** True when the webhook base URL points at this machine, so providers on the internet can't reach it. */
export function isLocalUrl(url: string): boolean {
  try {
    return LOCAL_HOSTS.has(new URL(url).hostname.toLowerCase());
  } catch {
    return false;
  }
}

export const CONNECTIVITY_LABEL: Record<NonNullable<Provider["connectivity"]>, string> = { api: "Cloud", sip: "SIP trunk" };
