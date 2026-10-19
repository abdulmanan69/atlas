import { useQuery } from "@tanstack/react-query";

import { api, unwrap } from "@/lib/api";

/** Deployment-wide sign-in options (whether self-service sign-up is open). */
export function useAuthOptions() {
  return useQuery({
    queryKey: ["auth-options"],
    queryFn: () => unwrap(api.GET("/api/v1/auth/options")),
    staleTime: 5 * 60_000,
  });
}
