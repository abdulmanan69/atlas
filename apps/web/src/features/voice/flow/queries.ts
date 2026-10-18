import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import { api, unwrap, type Schemas } from "@/lib/api";

export type ActionSummary = Pick<Schemas["ActionOut"], "id" | "name" | "function_name" | "description">;
export type DocumentSummary = Pick<Schemas["DocumentOut"], "id" | "filename" | "status" | "ready">;

export const fk = {
  actions: (o: string | null) => ["org", o, "actions"] as const,
  documents: (o: string | null) => ["org", o, "knowledge", "documents"] as const,
  compiledPrompt: (o: string | null, agent: string, version: number, node: string | null) => ["org", o, "agent", agent, "compiled-prompt", version, node] as const,
};

export function useActions(enabled = true) {
  const o = useOrgId();
  return useQuery({
    queryKey: fk.actions(o),
    queryFn: () => unwrap(api.GET("/api/v1/actions")),
    staleTime: 60_000,
    retry: false,
    enabled: !!o && enabled,
  });
}

export function useDocuments(enabled = true) {
  const o = useOrgId();
  return useQuery({
    queryKey: fk.documents(o),
    queryFn: () => unwrap(api.GET("/api/v1/knowledge/documents")),
    staleTime: 60_000,
    retry: false,
    enabled: !!o && enabled,
  });
}

/** What the model is told at a node, from the last saved version. */
export function useCompiledPrompt(agentId: string, version: number, nodeId: string | null, enabled: boolean) {
  const o = useOrgId();
  return useQuery({
    queryKey: fk.compiledPrompt(o, agentId, version, nodeId),
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/agents/{agent_id}/compiled-prompt", {
          params: { path: { agent_id: agentId }, query: nodeId ? { node_id: nodeId } : {} },
        }),
      ),
    staleTime: 5 * 60_000,
    enabled: !!o && enabled,
  });
}

/** action id → function name, for the instant path-name collision check. */
export function actionNameMap(actions: ActionSummary[] | undefined): Record<string, string> {
  const map: Record<string, string> = {};
  for (const a of actions ?? []) map[a.id] = a.function_name;
  return map;
}
