/**
 * Hover/focus prefetch for the busiest sidebar routes: load the route's code chunk and
 * warm the first query the page will run, so the click renders from cache.
 *
 * Every query key and fetcher here must equal what the page itself uses; the page then
 * finds fresh data and skips its own request. Block and page sizes are repeated rather
 * than imported because the modules that own them pull AG Grid into the main chunk
 * (`prefetch.test.ts` keeps them in sync).
 */
import { useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import { qk, useOrgId } from "@/features/queries";
import { api, unwrap } from "@/lib/api";

/** = leads-grid-model BLOCK_SIZE and calls-model BLOCK_SIZE. */
export const GRID_BLOCK = 50;
/** = campaigns-page PAGE_SIZE. */
export const CAMPAIGNS_PAGE = 24;
const FRESH_MS = 30_000;

interface RoutePrefetch {
  chunk: () => Promise<unknown>;
  warm: (qc: QueryClient, org: string) => Promise<void>;
}

export const ROUTE_PREFETCH: Record<string, RoutePrefetch> = {
  "/leads": {
    chunk: () => import("@/features/crm/leads-page"),
    warm: (qc, o) => {
      const query = { sort: "-created_at" as const, page: 1, page_size: GRID_BLOCK };
      return qc.prefetchQuery({
        queryKey: qk.leads(o, query),
        queryFn: () => unwrap(api.GET("/api/v1/leads", { params: { query } })),
        staleTime: FRESH_MS,
      });
    },
  },
  "/calls": {
    chunk: () => import("@/features/calls/calls-page"),
    warm: (qc, o) => {
      const query = { page: 1, page_size: GRID_BLOCK };
      return qc.prefetchQuery({
        queryKey: qk.calls(o, query),
        queryFn: () => unwrap(api.GET("/api/v1/calls", { params: { query } })),
        staleTime: 15_000,
      });
    },
  },
  "/campaigns": {
    chunk: () => import("@/features/campaigns/campaigns-page"),
    warm: (qc, o) => {
      const query = { page: 1, page_size: CAMPAIGNS_PAGE };
      return qc.prefetchQuery({
        queryKey: qk.campaigns(o, query),
        queryFn: () => unwrap(api.GET("/api/v1/campaigns", { params: { query } })),
        staleTime: FRESH_MS,
      });
    },
  },
  "/reports": {
    chunk: () => import("@/features/reports/reports-page"),
    warm: (qc, o) => {
      const query = { range_days: 30 };
      return qc.prefetchQuery({
        queryKey: qk.callsOverview(o, query),
        queryFn: () => unwrap(api.GET("/api/v1/analytics/calls-overview", { params: { query } })),
        staleTime: FRESH_MS,
      });
    },
  },
  "/agents": {
    chunk: () => import("@/features/voice/agents-page"),
    warm: (qc, o) => {
      const query = { include_archived: false };
      return qc.prefetchQuery({
        queryKey: qk.agents(o, false),
        queryFn: () => unwrap(api.GET("/api/v1/agents", { params: { query } })),
        staleTime: FRESH_MS,
      });
    },
  },
};

/** Start loading a route's chunk and first query. Safe to call repeatedly: both dedupe. */
export function prefetchRoute(path: string, qc: QueryClient, org: string | null): void {
  const entry = ROUTE_PREFETCH[path];
  if (!entry) return;
  // A failed chunk load is retried by the router on click; nothing to report here.
  entry.chunk().catch(() => undefined);
  if (org) void entry.warm(qc, org);
}

const HOVER_INTENT_MS = 60;

/**
 * Props for a nav link that prefetches its route on hover (after a short intent delay, so
 * sweeping the pointer across the sidebar doesn't fire every route) or on keyboard focus.
 */
export function usePrefetchIntent(): (path: string) => {
  onPointerEnter?: () => void;
  onPointerLeave?: () => void;
  onFocus?: () => void;
} {
  const qc = useQueryClient();
  const org = useOrgId();
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancel = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  useEffect(() => cancel, [cancel]);
  return useCallback(
    (path: string) =>
      ROUTE_PREFETCH[path]
        ? {
            onPointerEnter: () => {
              cancel();
              timer.current = setTimeout(() => prefetchRoute(path, qc, org), HOVER_INTENT_MS);
            },
            onPointerLeave: cancel,
            onFocus: () => prefetchRoute(path, qc, org),
          }
        : {},
    [qc, org, cancel],
  );
}
