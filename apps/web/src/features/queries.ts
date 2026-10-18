/**
 * Shared server-state hooks. Every key starts with ["org", orgId] so switching
 * organization or signing out can never surface another tenant's cached data.
 */
import { keepPreviousData, useQuery, useQueryClient } from "@tanstack/react-query";
import { useMemo } from "react";

import { api, unwrap, type Schemas } from "@/lib/api";
import { useHasRole, useSession } from "@/lib/session";
import type { paths } from "@/types/api";

export type Lead = Schemas["LeadOut"];
export type Stage = Schemas["StageOut"];
export type Member = Schemas["MemberOut"];
export type Task = Schemas["TaskOut"];
export type Activity = Schemas["ActivityOut"];
export type LeadQuery = NonNullable<paths["/api/v1/leads"]["get"]["parameters"]["query"]>;
export type TaskQuery = NonNullable<paths["/api/v1/tasks"]["get"]["parameters"]["query"]>;
export type Agent = Schemas["AgentOut"];
export type Call = Schemas["CallOut"];
export type CallDetail = Schemas["CallDetail"];
export type CallEvent = Schemas["CallEventOut"];
export type CallQuery = NonNullable<paths["/api/v1/calls"]["get"]["parameters"]["query"]>;
export type Campaign = Schemas["CampaignOut"];
export type CampaignQuery = NonNullable<paths["/api/v1/campaigns"]["get"]["parameters"]["query"]>;
export type CampaignLead = Schemas["CampaignLeadOut"];
export type CampaignLeadQuery = NonNullable<paths["/api/v1/campaigns/{campaign_id}/leads"]["get"]["parameters"]["query"]>;
export type CampaignProgress = Schemas["CampaignProgress"];
export type CampaignPreview = Schemas["CampaignPreview"];
export type CallsOverview = Schemas["CallsOverview"];
export type CallsOverviewQuery = NonNullable<paths["/api/v1/analytics/calls-overview"]["get"]["parameters"]["query"]>;

export function useOrgId(): string | null {
  return useSession((s) => s.orgId);
}

export const qk = {
  org: (o: string | null) => ["org", o] as const,
  stages: (o: string | null) => ["org", o, "stages"] as const,
  members: (o: string | null) => ["org", o, "members"] as const,
  invitations: (o: string | null) => ["org", o, "invitations"] as const,
  leads: (o: string | null, q?: LeadQuery) => ["org", o, "leads", q ?? {}] as const,
  lead: (o: string | null, id: string) => ["org", o, "lead", id] as const,
  timeline: (o: string | null, id: string) => ["org", o, "lead", id, "timeline"] as const,
  tasks: (o: string | null, q?: TaskQuery) => ["org", o, "tasks", q ?? {}] as const,
  overview: (o: string | null, days: number) => ["org", o, "overview", days] as const,
  audit: (o: string | null, q: object) => ["org", o, "audit", q] as const,
  organization: (o: string | null) => ["org", o, "organization"] as const,
  // Same shape as the voice feature's agents key so both share one cache entry.
  agents: (o: string | null, archived: boolean) => ["org", o, "agents", archived] as const,
  calls: (o: string | null, q?: CallQuery) => ["org", o, "calls", q ?? {}] as const,
  call: (o: string | null, id: string) => ["org", o, "call", id] as const,
  callEvents: (o: string | null, id: string) => ["org", o, "call", id, "events"] as const,
  callTranscript: (o: string | null, id: string) => ["org", o, "call", id, "transcript"] as const,
  campaigns: (o: string | null, q?: CampaignQuery) => ["org", o, "campaigns", q ?? {}] as const,
  campaign: (o: string | null, id: string) => ["org", o, "campaign", id] as const,
  campaignProgress: (o: string | null, id: string) => ["org", o, "campaign", id, "progress"] as const,
  campaignPreview: (o: string | null, id: string, v: string) => ["org", o, "campaign", id, "preview", v] as const,
  campaignLeads: (o: string | null, id: string, q: CampaignLeadQuery) => ["org", o, "campaign", id, "leads", q] as const,
  callsOverview: (o: string | null, q: CallsOverviewQuery) => ["org", o, "calls-overview", q] as const,
};

export function useStages() {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.stages(o),
    queryFn: () => unwrap(api.GET("/api/v1/pipeline-stages")),
    staleTime: 5 * 60_000,
    enabled: !!o,
  });
}

export function useMembers() {
  const o = useOrgId();
  const canRead = useHasRole("agent");
  return useQuery({
    queryKey: qk.members(o),
    queryFn: () => unwrap(api.GET("/api/v1/members")),
    staleTime: 60_000,
    enabled: !!o && canRead,
  });
}

/** user_id → display name, for owners, assignees and timeline actors. */
export function useMemberNames(): Map<string, string> {
  const { data } = useMembers();
  const me = useSession((s) => s.user);
  return useMemo(() => {
    const map = new Map<string, string>();
    for (const m of data ?? []) map.set(m.user.id, m.user.full_name);
    if (me) map.set(me.id, me.full_name);
    return map;
  }, [data, me]);
}

export function useLeads(query: LeadQuery) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.leads(o, query),
    queryFn: () => unwrap(api.GET("/api/v1/leads", { params: { query } })),
    placeholderData: keepPreviousData,
    enabled: !!o,
  });
}

export function useLead(id: string) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.lead(o, id),
    queryFn: () => unwrap(api.GET("/api/v1/leads/{lead_id}", { params: { path: { lead_id: id } } })),
    enabled: !!o,
  });
}

export function useTasks(query: TaskQuery) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.tasks(o, query),
    queryFn: () => unwrap(api.GET("/api/v1/tasks", { params: { query } })),
    placeholderData: keepPreviousData,
    enabled: !!o,
  });
}

/** Every agent of the organization (archived ones too when asked), for pickers and name lookups. */
export function useAgents(includeArchived = false) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.agents(o, includeArchived),
    queryFn: () => unwrap(api.GET("/api/v1/agents", { params: { query: { include_archived: includeArchived } } })),
    staleTime: 60_000,
    enabled: !!o,
  });
}

/** agent_id → name, across archived agents too, so old calls still show who handled them. */
export function useAgentNames(): Map<string, string> {
  const { data } = useAgents(true);
  return useMemo(() => new Map((data ?? []).map((a) => [a.id, a.name])), [data]);
}

export function useCall(id: string, opts: { refetchInterval?: number | false | ((state: CallDetail | undefined) => number | false) } = {}) {
  const o = useOrgId();
  const interval = opts.refetchInterval;
  return useQuery({
    queryKey: qk.call(o, id),
    queryFn: () => unwrap(api.GET("/api/v1/calls/{call_id}", { params: { path: { call_id: id } } })),
    refetchInterval: typeof interval === "function" ? (q) => interval(q.state.data) : (interval ?? false),
    enabled: !!o && !!id,
  });
}

export function useCallEvents(id: string) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.callEvents(o, id),
    queryFn: () => unwrap(api.GET("/api/v1/calls/{call_id}/events", { params: { path: { call_id: id } } })),
    enabled: !!o && !!id,
  });
}

export function useCampaigns(query: CampaignQuery) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.campaigns(o, query),
    queryFn: () => unwrap(api.GET("/api/v1/campaigns", { params: { query } })),
    placeholderData: keepPreviousData,
    enabled: !!o,
  });
}

export function useCampaign(id: string, opts: { refetchInterval?: number | false | ((data: Campaign | undefined) => number | false) } = {}) {
  const o = useOrgId();
  const interval = opts.refetchInterval;
  return useQuery({
    queryKey: qk.campaign(o, id),
    queryFn: () => unwrap(api.GET("/api/v1/campaigns/{campaign_id}", { params: { path: { campaign_id: id } } })),
    refetchInterval: typeof interval === "function" ? (q) => interval(q.state.data) : (interval ?? false),
    enabled: !!o && !!id,
  });
}

export function useCampaignProgress(id: string, opts: { refetchInterval?: number | false } = {}) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.campaignProgress(o, id),
    queryFn: () => unwrap(api.GET("/api/v1/campaigns/{campaign_id}/progress", { params: { path: { campaign_id: id } } })),
    refetchInterval: opts.refetchInterval ?? false,
    enabled: !!o && !!id,
  });
}

export function useCampaignLeads(id: string, query: CampaignLeadQuery, opts: { refetchInterval?: number | false } = {}) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.campaignLeads(o, id, query),
    queryFn: () => unwrap(api.GET("/api/v1/campaigns/{campaign_id}/leads", { params: { path: { campaign_id: id }, query } })),
    placeholderData: keepPreviousData,
    refetchInterval: opts.refetchInterval ?? false,
    enabled: !!o && !!id,
  });
}

export function useCallsOverview(query: CallsOverviewQuery, enabled = true) {
  const o = useOrgId();
  return useQuery({
    queryKey: qk.callsOverview(o, query),
    queryFn: () => unwrap(api.GET("/api/v1/analytics/calls-overview", { params: { query } })),
    placeholderData: keepPreviousData,
    staleTime: 30_000,
    enabled: !!o && enabled,
  });
}

/** Invalidate everything for the current organization after a write. */
export function useInvalidate() {
  const qc = useQueryClient();
  const o = useOrgId();
  return (...parts: string[]) => qc.invalidateQueries({ queryKey: [...qk.org(o), ...parts] });
}
