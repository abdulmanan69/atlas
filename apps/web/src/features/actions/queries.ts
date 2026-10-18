import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import { api, unwrap } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export const ak = {
  actions: (o: string | null) => ["org", o, "actions"] as const,
};

export function useActions() {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({ queryKey: ak.actions(o), queryFn: () => unwrap(api.GET("/api/v1/actions")), enabled: !!o && can });
}
