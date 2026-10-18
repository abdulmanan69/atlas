import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";
import type { paths } from "@/types/api";

export type ApiKey = Schemas["ApiKeyOut"];
export type ApiKeyCreated = Schemas["ApiKeyCreated"];
export type Webhook = Schemas["WebhookEndpointOut"];
export type WebhookCreated = Schemas["WebhookEndpointCreated"];
export type Delivery = Schemas["WebhookDeliveryOut"];
export type DeliveryStatus = Schemas["DeliveryStatus"];
export type EventType = Schemas["EventTypeOut"];
export type DeliveryQuery = NonNullable<paths["/api/v1/developer/webhooks/{endpoint_id}/deliveries"]["get"]["parameters"]["query"]>;

export const WILDCARD = "*";

export const dk = {
  apiKeys: (o: string | null, revoked: boolean) => ["org", o, "developer", "api-keys", revoked] as const,
  webhooks: (o: string | null) => ["org", o, "developer", "webhooks"] as const,
  events: (o: string | null) => ["org", o, "developer", "events"] as const,
  deliveries: (o: string | null, id: string, q: DeliveryQuery) => ["org", o, "developer", "webhooks", id, "deliveries", q] as const,
};

export function useApiKeys(includeRevoked: boolean) {
  const o = useOrgId();
  const can = useHasRole("admin");
  return useQuery({
    queryKey: dk.apiKeys(o, includeRevoked),
    queryFn: () => unwrap(api.GET("/api/v1/developer/api-keys", { params: { query: { include_revoked: includeRevoked } } })),
    placeholderData: keepPreviousData,
    enabled: !!o && can,
  });
}

export function useWebhooks() {
  const o = useOrgId();
  const can = useHasRole("admin");
  return useQuery({ queryKey: dk.webhooks(o), queryFn: () => unwrap(api.GET("/api/v1/developer/webhooks")), enabled: !!o && can });
}

export function useEventTypes() {
  const o = useOrgId();
  const can = useHasRole("admin");
  return useQuery({ queryKey: dk.events(o), queryFn: () => unwrap(api.GET("/api/v1/developer/events")), staleTime: 10 * 60_000, enabled: !!o && can });
}

export function useDeliveries(endpointId: string | null, query: DeliveryQuery) {
  const o = useOrgId();
  return useQuery({
    queryKey: dk.deliveries(o, endpointId ?? "", query),
    queryFn: () => unwrap(api.GET("/api/v1/developer/webhooks/{endpoint_id}/deliveries", { params: { path: { endpoint_id: endpointId! }, query } })),
    placeholderData: keepPreviousData,
    // Pending deliveries resolve within seconds; keep the list honest while it is open.
    refetchInterval: (q) => (q.state.data?.items.some((d) => d.status === "pending") ? 5000 : false),
    enabled: !!o && !!endpointId,
  });
}

export function keyState(k: ApiKey, now = Date.now()): "active" | "expired" | "revoked" {
  if (k.revoked_at) return "revoked";
  if (k.expires_at && new Date(k.expires_at).getTime() <= now) return "expired";
  return "active";
}
