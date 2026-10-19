import { hashKey, QueryClient } from "@tanstack/react-query";

import { CAMPAIGNS_PAGE, GRID_BLOCK, ROUTE_PREFETCH } from "@/app/prefetch";
import { BLOCK_SIZE as CALLS_BLOCK, blockToQuery as callsBlock, readCallsUrl, urlToFilters } from "@/features/calls/calls-model";
import campaignsSource from "@/features/campaigns/campaigns-page.tsx?raw";
import { BLOCK_SIZE as LEADS_BLOCK, blockToQuery as leadsBlock } from "@/features/crm/leads-grid-model";
import { qk } from "@/features/queries";

vi.mock("@/lib/api", () => ({
  api: { GET: vi.fn(async (path: string) => ({ data: { path }, response: new Response() })) },
  unwrap: async (p: Promise<{ data: unknown }>) => (await p).data,
}));

const ORG = "org-1";

describe("route prefetch", () => {
  it("uses the same page sizes as the pages", () => {
    expect(GRID_BLOCK).toBe(LEADS_BLOCK);
    expect(GRID_BLOCK).toBe(CALLS_BLOCK);
    expect(campaignsSource).toMatch(new RegExp(`const PAGE_SIZE = ${CAMPAIGNS_PAGE};`));
  });

  it("warms exactly the cache entries the pages read first", async () => {
    const qc = new QueryClient();
    await Promise.all(Object.values(ROUTE_PREFETCH).map((r) => r.warm(qc, ORG)));

    // What each page asks for on first render with no URL filters.
    const leadsFilters = { q: undefined, stage_id: undefined, owner_user_id: undefined, unassigned: undefined, do_not_call: undefined, tag: undefined };
    const expected = [
      qk.leads(ORG, leadsBlock({ startRow: 0, endRow: LEADS_BLOCK }, leadsFilters, "-created_at")),
      qk.calls(ORG, callsBlock({ startRow: 0, endRow: CALLS_BLOCK }, urlToFilters(readCallsUrl(new URLSearchParams())))),
      qk.campaigns(ORG, { status: undefined, page: 1, page_size: CAMPAIGNS_PAGE }),
      qk.callsOverview(ORG, { range_days: 30, agent_id: undefined, campaign_id: undefined, mode: undefined }),
      qk.agents(ORG, false),
    ];
    const cached = new Set(qc.getQueryCache().getAll().map((q) => q.queryHash));
    for (const key of expected) expect(cached.has(hashKey(key))).toBe(true);
    expect(cached.size).toBe(expected.length);
  });
});
