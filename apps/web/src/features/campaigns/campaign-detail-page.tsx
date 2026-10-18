import { useMutation } from "@tanstack/react-query";
import { ArrowLeft, Bot, MoreHorizontal, Pause, Pencil, Play, Rocket, Trash2, XCircle } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate, useParams, useSearchParams } from "react-router";
import { toast } from "sonner";

import { EmptyState, ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { MenuContent, MenuItem, MenuRoot, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card, CardHeader, Skeleton, Tooltip } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { Pagination, Table, Td, Th, Tr } from "@/components/ui/table";
import { outcomeLabel, outcomeTone } from "@/features/calls/calls-model";
import { CampaignWizard } from "@/features/campaigns/campaign-wizard";
import {
  canTransition,
  formatEta,
  isActive,
  isTerminal,
  leadCounts,
  LEAD_STATUS,
  LEAD_STATUS_ORDER,
  MODE_BADGE,
  skipReasonLabel,
  STATUS_BADGE,
  summarizeHours,
  totalLeads,
  type CampaignLeadStatus,
  type LifecycleAction,
} from "@/features/campaigns/campaigns-model";
import { useAgentNames, useCampaign, useCampaignLeads, useCampaignProgress, useInvalidate, useMemberNames, useStages, type Campaign, type CampaignProgress } from "@/features/queries";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatDateTime, formatDuration, formatNumber, formatPercent, formatRelative, fullName } from "@/lib/format";
import { useHasRole } from "@/lib/session";
import { cn } from "@/lib/utils";

const POLL_MS = 10_000;
const LEADS_PAGE = 50;

export default function CampaignDetailPage() {
  const { campaignId = "" } = useParams();
  // Poll while the campaign is scheduled or running; finished and draft campaigns don't change by themselves.
  const campaign = useCampaign(campaignId, { refetchInterval: (data) => (data && isActive(data.status) ? POLL_MS : false) });

  if (campaign.isError) {
    return (
      <>
        <BackLink />
        <Card>
          <ErrorState error={campaign.error} onRetry={() => void campaign.refetch()} />
        </Card>
      </>
    );
  }
  if (!campaign.data) {
    return (
      <div className="flex flex-col gap-5">
        <Skeleton className="h-4 w-20" />
        <Skeleton className="h-9 w-80" />
        <div className="grid gap-5 lg:grid-cols-3">
          <Skeleton className="h-64 lg:col-span-2" />
          <Skeleton className="h-64" />
        </div>
      </div>
    );
  }
  return <CampaignView campaign={campaign.data} />;
}

function BackLink() {
  return (
    <Link to="/campaigns" className="mb-4 inline-flex items-center gap-1 text-[12.5px] font-medium text-muted hover:text-ink">
      <ArrowLeft className="size-3.5" /> Campaigns
    </Link>
  );
}

const ACTIONS: Record<LifecycleAction, { label: string; title: (name: string) => string; description: string; confirm: string; tone: "danger" | "primary"; icon: typeof Play }> = {
  start: {
    label: "Start",
    title: (n) => `Start “${n}”?`,
    description: "The audience is snapshotted now and calls begin within the calling hours. A dry run only places simulated calls.",
    confirm: "Start campaign",
    tone: "primary",
    icon: Rocket,
  },
  pause: {
    label: "Pause",
    title: (n) => `Pause “${n}”?`,
    description: "No new calls are placed. Calls already in progress finish normally. You can resume at any time.",
    confirm: "Pause campaign",
    tone: "primary",
    icon: Pause,
  },
  resume: {
    label: "Resume",
    title: (n) => `Resume “${n}”?`,
    description: "Calling continues from where it stopped. The agent and the live-calling gates are checked again first.",
    confirm: "Resume campaign",
    tone: "primary",
    icon: Play,
  },
  cancel: {
    label: "Cancel",
    title: (n) => `Cancel “${n}”?`,
    description: "Pending leads are skipped and the campaign ends. Calls already in progress are still recorded. This can't be undone.",
    confirm: "Cancel campaign",
    tone: "danger",
    icon: XCircle,
  },
};

function CampaignView({ campaign }: { campaign: Campaign }) {
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const canManage = useHasRole("manager");
  const names = useAgentNames();
  const polling = isActive(campaign.status) ? POLL_MS : false;
  const progress = useCampaignProgress(campaign.id, { refetchInterval: polling });
  const [confirming, setConfirming] = useState<LifecycleAction | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [editing, setEditing] = useState(false);

  const act = useMutation({
    mutationFn: (action: LifecycleAction) =>
      unwrap(api.POST(`/api/v1/campaigns/{campaign_id}/${action}` as "/api/v1/campaigns/{campaign_id}/start", { params: { path: { campaign_id: campaign.id } } })),
    onSuccess: (c, action) => {
      toast.success(action === "cancel" ? "Campaign canceled" : `Campaign ${STATUS_BADGE[c.status].label.toLowerCase()}`);
      setConfirming(null);
      void invalidate("campaigns");
      void invalidate("campaign", campaign.id);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/campaigns/{campaign_id}", { params: { path: { campaign_id: campaign.id } } })),
    onSuccess: () => {
      toast.success("Campaign deleted");
      void invalidate("campaigns");
      void navigate("/campaigns");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const status = STATUS_BADGE[campaign.status];
  const mode = MODE_BADGE[campaign.mode];
  const agentName = names.get(campaign.agent_id);
  const visible: LifecycleAction[] = (["start", "resume", "pause", "cancel"] as LifecycleAction[]).filter((a) => canTransition(campaign.status, a));

  return (
    <>
      <BackLink />
      <header className="flex flex-wrap items-start justify-between gap-4 pb-5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="title-display text-[28px]">{campaign.name}</h1>
            <Badge tone={status.tone}>{status.label}</Badge>
            <Tooltip content={mode.description}>
              <span>
                <Badge tone={mode.tone} className="cursor-help">
                  {mode.label}
                </Badge>
              </span>
            </Tooltip>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[13.5px] text-muted">
            <Link to={`/agents/${campaign.agent_id}`} className="inline-flex items-center gap-1.5 hover:text-ink">
              <Bot className="size-3.5" aria-hidden /> {agentName ?? "Agent"}
            </Link>
            {campaign.started_at ? (
              <>
                <span aria-hidden>·</span>
                <span title={formatDateTime(campaign.started_at)}>Started {formatRelative(campaign.started_at)}</span>
              </>
            ) : campaign.start_at ? (
              <>
                <span aria-hidden>·</span>
                <span>Starts {formatDateTime(campaign.start_at)}</span>
              </>
            ) : null}
            {campaign.finished_at ? (
              <>
                <span aria-hidden>·</span>
                <span title={formatDateTime(campaign.finished_at)}>Finished {formatRelative(campaign.finished_at)}</span>
              </>
            ) : null}
          </div>
          {campaign.objective ? <p className="mt-2 max-w-2xl text-[13.5px] text-ink-2">{campaign.objective}</p> : null}
        </div>
        {canManage ? (
          <div className="flex flex-wrap items-center gap-2">
            {visible.map((a) => {
              const def = ACTIONS[a];
              const Icon = def.icon;
              return (
                <Button key={a} variant={a === "start" || a === "resume" ? "primary" : a === "cancel" ? "ghost" : "secondary"} className={cn(a === "cancel" && "text-danger hover:text-danger")} onClick={() => setConfirming(a)}>
                  <Icon /> {def.label}
                </Button>
              );
            })}
            {!isTerminal(campaign.status) ? (
              <Button onClick={() => setEditing(true)}>
                <Pencil /> Edit
              </Button>
            ) : null}
            {campaign.status === "draft" || isTerminal(campaign.status) ? (
              <MenuRoot>
                <MenuTrigger asChild>
                  <Button size="icon" aria-label="More actions">
                    <MoreHorizontal />
                  </Button>
                </MenuTrigger>
                <MenuContent>
                  <MenuItem tone="danger" onSelect={() => setDeleting(true)}>
                    <Trash2 /> Delete campaign
                  </MenuItem>
                </MenuContent>
              </MenuRoot>
            ) : null}
          </div>
        ) : null}
      </header>

      {campaign.last_error ? (
        <div role="alert" className="mb-5 rounded-md border border-danger/30 bg-danger-soft px-3 py-2.5 text-[13px] text-danger">
          <span className="font-medium">Needs attention.</span> {campaign.last_error}
        </div>
      ) : null}
      {campaign.backoff_until && new Date(campaign.backoff_until).getTime() > Date.now() ? (
        <div role="status" className="mb-5 rounded-md border border-warn/30 bg-warn-soft px-3 py-2.5 text-[13px] text-warn">
          Dispatching paused until {formatDateTime(campaign.backoff_until)} because the voice service reported it was at capacity.
        </div>
      ) : null}

      <div className="grid gap-5 lg:grid-cols-3">
        <div className="flex min-w-0 flex-col gap-5 lg:col-span-2">
          <ProgressCard campaign={campaign} progress={progress.data} error={progress.error} onRetry={() => void progress.refetch()} />
          <LeadsCard campaign={campaign} polling={polling} />
        </div>
        <div className="flex flex-col gap-5">
          <OutcomesCard progress={progress.data} />
          <SettingsCard campaign={campaign} agentName={agentName} onEdit={canManage && !isTerminal(campaign.status) ? () => setEditing(true) : undefined} />
        </div>
      </div>

      {confirming ? (
        <ConfirmDialog
          open
          onOpenChange={(o) => !o && setConfirming(null)}
          title={ACTIONS[confirming].title(campaign.name)}
          description={
            confirming === "start" && campaign.mode === "live"
              ? "This is a LIVE campaign: real phone calls will be placed to every eligible lead. " + ACTIONS.start.description
              : ACTIONS[confirming].description
          }
          confirmLabel={ACTIONS[confirming].confirm}
          tone={ACTIONS[confirming].tone}
          loading={act.isPending}
          onConfirm={() => act.mutate(confirming)}
        />
      ) : null}
      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title={`Delete “${campaign.name}”?`}
        description="The campaign and its audience snapshot are removed. Call records and lead timelines are kept."
        confirmLabel="Delete campaign"
        loading={remove.isPending}
        onConfirm={() => remove.mutate()}
      />
      <CampaignWizard open={editing} onOpenChange={setEditing} campaign={campaign} />
    </>
  );
}

function ProgressCard({ campaign, progress, error, onRetry }: { campaign: Campaign; progress: CampaignProgress | undefined; error: unknown; onRetry: () => void }) {
  const counts = leadCounts(progress, campaign);
  const total = totalLeads(counts);
  const eta = formatEta(progress?.eta_seconds);
  return (
    <Card>
      <CardHeader title="Progress" description={isActive(campaign.status) ? "Refreshes every 10 seconds while the campaign runs" : "Where every lead in the audience ended up"} />
      {error ? (
        <ErrorState error={error} onRetry={onRetry} />
      ) : (
        <div className="px-4 py-4">
          {total === 0 ? (
            <p className="text-[13px] text-muted">{campaign.status === "draft" ? "The audience is snapshotted when the campaign starts." : "No leads were in the audience."}</p>
          ) : (
            <>
              <div className="flex h-3 w-full gap-px overflow-hidden rounded-full bg-surface-2" role="img" aria-label={LEAD_STATUS_ORDER.map((s) => `${LEAD_STATUS[s].label} ${counts[s]}`).join(", ")}>
                {LEAD_STATUS_ORDER.map((s) => (counts[s] > 0 ? <span key={s} className={cn("h-full", LEAD_STATUS[s].bar)} style={{ width: `${(counts[s] / total) * 100}%` }} /> : null))}
              </div>
              <ul className="mt-3 grid grid-cols-3 gap-x-4 gap-y-2 sm:grid-cols-6">
                {LEAD_STATUS_ORDER.map((s) => (
                  <li key={s} className="min-w-0" title={LEAD_STATUS[s].description}>
                    <div className="flex items-center gap-1.5 text-[12px] text-muted">
                      <span className={cn("size-2 shrink-0 rounded-sm", LEAD_STATUS[s].bar)} aria-hidden />
                      <span className="truncate">{LEAD_STATUS[s].label}</span>
                    </div>
                    <div className={cn("mt-0.5 font-mono text-[16px] font-medium leading-none", counts[s] === 0 ? "text-faint" : "text-ink")}>{formatNumber(counts[s])}</div>
                  </li>
                ))}
              </ul>
            </>
          )}
          <dl className="mt-5 grid grid-cols-2 gap-4 border-t border-line pt-4 sm:grid-cols-4">
            <Metric label="Calls placed" value={progress ? formatNumber(progress.calls_total) : null} />
            <Metric label="Answer rate" value={progress ? formatPercent(progress.answer_rate) : null} note="Answered ÷ finished calls" />
            <Metric label="Avg. duration" value={progress ? formatDuration(progress.average_duration_seconds) : null} note="Answered calls" />
            <Metric
              label={campaign.status === "running" ? "Time left" : "Finished"}
              value={campaign.status === "running" ? (progress ? (eta ?? "—") : null) : campaign.finished_at ? formatRelative(campaign.finished_at) : "—"}
              note={campaign.status === "running" ? "Rough estimate from pacing" : undefined}
            />
          </dl>
        </div>
      )}
    </Card>
  );
}

function Metric({ label, value, note }: { label: string; value: string | null; note?: string }) {
  return (
    <div>
      <dt className="eyebrow">{label}</dt>
      <dd className="mt-1.5 font-mono text-[22px] font-medium leading-none tracking-tight text-ink">{value === null ? <Skeleton className="h-5 w-14" /> : value}</dd>
      {note ? <dd className="mt-1 text-[11.5px] text-faint">{note}</dd> : null}
    </div>
  );
}

function OutcomesCard({ progress }: { progress: CampaignProgress | undefined }) {
  const rows = Object.entries(progress?.calls_by_outcome ?? {}).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...rows.map(([, n]) => n));
  return (
    <Card>
      <CardHeader title="Outcomes" description="Answered calls by the agent's outcome code" />
      {!progress ? (
        <div className="flex flex-col gap-2 p-4">
          <Skeleton className="h-6" />
          <Skeleton className="h-6" />
        </div>
      ) : rows.length === 0 ? (
        <p className="px-4 py-5 text-[13px] text-muted">No outcomes recorded yet.</p>
      ) : (
        <ul className="flex flex-col gap-2.5 px-4 py-4">
          {rows.map(([code, n]) => (
            <li key={code}>
              <div className="flex items-baseline justify-between text-[12.5px]">
                <Badge tone={outcomeTone(code)}>{outcomeLabel(code)}</Badge>
                <span className="font-mono text-ink-2">{formatNumber(n)}</span>
              </div>
              <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
                <div className={cn("h-full rounded-full", outcomeTone(code) === "success" ? "bg-success" : outcomeTone(code) === "danger" ? "bg-danger" : "bg-accent")} style={{ width: `${(n / max) * 100}%` }} />
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function LeadsCard({ campaign, polling }: { campaign: Campaign; polling: number | false }) {
  const [params, setParams] = useSearchParams();
  const navigate = useNavigate();
  const status = (LEAD_STATUS_ORDER as string[]).includes(params.get("leads") ?? "") ? (params.get("leads") as CampaignLeadStatus) : null;
  const page = Number(params.get("page") ?? 1) || 1;
  const leads = useCampaignLeads(campaign.id, { status: status ?? undefined, page, page_size: LEADS_PAGE }, { refetchInterval: polling });
  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v === null || v === "") next.delete(k);
    else next.set(k, v);
    if (k !== "page") next.delete("page");
    setParams(next, { replace: true });
  };

  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="Leads"
        description="The audience snapshot with each lead's status"
        action={
          <Select aria-label="Lead status" size="sm" value={status ?? ""} onValueChange={(v) => set("leads", v)} allowEmpty emptyLabel="All statuses" className="w-auto min-w-32">
            {LEAD_STATUS_ORDER.map((s) => (
              <SelectItem key={s} value={s}>
                {LEAD_STATUS[s].label}
              </SelectItem>
            ))}
          </Select>
        }
      />
      {leads.isError ? (
        <ErrorState error={leads.error} onRetry={() => void leads.refetch()} />
      ) : leads.data && leads.data.items.length === 0 ? (
        <EmptyState title={status ? `No ${LEAD_STATUS[status].label.toLowerCase()} leads` : campaign.status === "draft" ? "The audience is snapshotted when the campaign starts" : "No leads in this campaign"} className="py-10" />
      ) : (
        <>
          <Table>
            <thead>
              <tr>
                <Th>Lead</Th>
                <Th>Status</Th>
                <Th className="text-right">Attempts</Th>
                <Th>Last outcome</Th>
                <Th>Next / reason</Th>
                <Th>Updated</Th>
              </tr>
            </thead>
            <tbody>
              {leads.data
                ? leads.data.items.map((item) => {
                    const s = LEAD_STATUS[item.status];
                    return (
                      <Tr
                        key={item.id}
                        className="cursor-pointer"
                        onClick={(e) => {
                          if ((e.target as HTMLElement).closest("a")) return;
                          void navigate(`/leads/${item.lead_id}`);
                        }}
                      >
                        <Td>
                          <div className="min-w-0 leading-tight">
                            <Link to={`/leads/${item.lead_id}`} className="block truncate font-semibold hover:text-accent">
                              {item.lead ? fullName(item.lead.first_name, item.lead.last_name) : "Lead removed"}
                            </Link>
                            <span className="block truncate font-mono text-[12px] text-muted">{item.lead?.phone_e164 ?? item.lead?.company ?? "—"}</span>
                          </div>
                        </Td>
                        <Td>
                          <Badge tone={s.tone}>{s.label}</Badge>
                        </Td>
                        <Td className="text-right font-mono text-[12.5px]">{item.attempts}</Td>
                        <Td>
                          {item.last_outcome ? (
                            item.last_call_id ? (
                              <Link to={`/calls/${item.last_call_id}`} className="inline-flex" title="Open the call">
                                <Badge tone={outcomeTone(item.last_outcome)}>{outcomeLabel(item.last_outcome)}</Badge>
                              </Link>
                            ) : (
                              <Badge tone={outcomeTone(item.last_outcome)}>{outcomeLabel(item.last_outcome)}</Badge>
                            )
                          ) : item.last_call_id ? (
                            <Link to={`/calls/${item.last_call_id}`} className="text-[12.5px] text-accent hover:underline">
                              Open call
                            </Link>
                          ) : (
                            <span className="text-faint">—</span>
                          )}
                        </Td>
                        <Td className="text-[12.5px] text-muted">
                          {item.status === "skipped" ? skipReasonLabel(item.skip_reason) : item.status === "pending" && item.next_attempt_at ? `Due ${formatRelative(item.next_attempt_at)}` : item.status === "pending" ? "Due now" : "—"}
                        </Td>
                        <Td className="text-[12.5px] text-muted">{formatRelative(item.updated_at)}</Td>
                      </Tr>
                    );
                  })
                : Array.from({ length: 5 }, (_, i) => (
                    <tr key={i}>
                      <Td colSpan={6}>
                        <Skeleton className="h-7" />
                      </Td>
                    </tr>
                  ))}
            </tbody>
          </Table>
          {leads.data ? <Pagination page={page} pageSize={LEADS_PAGE} total={leads.data.total} onPageChange={(p) => set("page", String(p))} /> : null}
        </>
      )}
    </Card>
  );
}

function SettingsCard({ campaign: c, agentName, onEdit }: { campaign: Campaign; agentName: string | undefined; onEdit?: () => void }) {
  const stages = useStages();
  const members = useMemberNames();
  const f = c.lead_filter;
  const audience = [
    f?.stage_id ? `Stage: ${stages.data?.find((s) => s.id === f.stage_id)?.name ?? "…"}` : null,
    f?.unassigned ? "Unassigned" : f?.owner_user_id ? `Owner: ${members.get(f.owner_user_id) ?? "Teammate"}` : null,
    f?.tag ? `Tag: ${f.tag}` : null,
    f?.source ? `Source: ${f.source}` : null,
    f?.qualification_status ? `Qualification: ${f.qualification_status}` : null,
    f?.q ? `Search: ${f.q}` : null,
    c.lead_ids?.length ? `${formatNumber(c.lead_ids.length)} hand-picked leads` : null,
  ].filter((x): x is string => Boolean(x));
  const retry = c.retry_policy;
  const rows: [string, React.ReactNode][] = [
    [
      "Agent",
      <Link key="agent" to={`/agents/${c.agent_id}`} className="text-accent hover:underline">
        {agentName ?? "Agent"}
      </Link>,
    ],
    ["Mode", <Badge key="mode" tone={MODE_BADGE[c.mode].tone}>{MODE_BADGE[c.mode].label}</Badge>],
    [
      "Audience",
      audience.length ? (
        <span key="aud" className="flex flex-wrap gap-1">
          {audience.map((a) => (
            <Badge key={a}>{a}</Badge>
          ))}
        </span>
      ) : (
        "—"
      ),
    ],
    ["Starts", c.start_at ? formatDateTime(c.start_at) : "As soon as started"],
    ["Ends", c.end_at ? formatDateTime(c.end_at) : "When done"],
    ["Time zone", c.timezone ?? "Organization default"],
    ["Calling hours", summarizeHours(c.calling_hours)],
    ["Pacing", `${c.max_concurrency} concurrent · ${c.calls_per_minute}/min${c.max_calls ? ` · max ${formatNumber(c.max_calls)} calls` : ""}`],
    ["Retries", `${retry.max_attempts ?? 1} attempts · on ${(retry.retry_on ?? []).map((r) => r.replace("_", " ")).join(", ") || "nothing"} · ${retry.backoff_minutes ?? 0} min backoff`],
    ["Created", formatDateTime(c.created_at)],
  ];
  return (
    <Card>
      <CardHeader
        title="Settings"
        action={
          onEdit ? (
            <Button size="sm" variant="ghost" onClick={onEdit}>
              <Pencil /> Edit
            </Button>
          ) : null
        }
      />
      <dl className="divide-y divide-line">
        {rows.map(([k, v]) => (
          <div key={k} className="grid grid-cols-[100px_minmax(0,1fr)] items-start gap-3 px-4 py-2.5">
            <dt className="text-[12.5px] text-muted">{k}</dt>
            <dd className="min-w-0 text-[13px] text-ink-2">{v}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}
