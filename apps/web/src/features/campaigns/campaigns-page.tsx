import { Bot, Megaphone, Plus, Search } from "lucide-react";
import { useState } from "react";
import { Link, useSearchParams } from "react-router";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, Skeleton, Tooltip } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { Pagination } from "@/components/ui/table";
import { CampaignWizard } from "@/features/campaigns/campaign-wizard";
import { leadCounts, LEAD_STATUS, LEAD_STATUS_ORDER, MODE_BADGE, settledLeads, STATUS_BADGE, totalLeads, type CampaignStatus } from "@/features/campaigns/campaigns-model";
import { useAgentNames, useCampaigns, type Campaign } from "@/features/queries";
import { formatDateTime, formatNumber, formatPercent, formatRelative } from "@/lib/format";
import { useHasRole } from "@/lib/session";
import { cn } from "@/lib/utils";

const PAGE_SIZE = 24;
const STATUSES: CampaignStatus[] = ["draft", "scheduled", "running", "paused", "completed", "canceled", "failed"];

export default function CampaignsPage() {
  const [params, setParams] = useSearchParams();
  const canManage = useHasRole("manager");
  const [creating, setCreating] = useState(false);
  const status = (STATUSES as string[]).includes(params.get("status") ?? "") ? (params.get("status") as CampaignStatus) : null;
  const page = Number(params.get("page") ?? 1) || 1;
  const campaigns = useCampaigns({ status: status ?? undefined, page, page_size: PAGE_SIZE });
  const names = useAgentNames();

  const set = (k: string, v: string | null) => {
    const next = new URLSearchParams(params);
    if (v === null || v === "") next.delete(k);
    else next.set(k, v);
    if (k !== "page") next.delete("page");
    setParams(next, { replace: true });
  };

  return (
    <>
      <PageHeader
        title="Campaigns"
        description="An agent calls an audience under explicit pacing, calling-hour and retry rules. Dry runs rehearse everything except the phone call."
        actions={
          canManage ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              <Plus /> New campaign
            </Button>
          ) : null
        }
      />
      <div className="mb-4 flex flex-wrap items-center gap-2">
        <Select aria-label="Status" value={status ?? ""} onValueChange={(v) => set("status", v)} allowEmpty emptyLabel="All statuses" className="w-auto min-w-40">
          {STATUSES.map((s) => (
            <SelectItem key={s} value={s}>
              {STATUS_BADGE[s].label}
            </SelectItem>
          ))}
        </Select>
      </div>

      {campaigns.isError ? (
        <Card>
          <ErrorState error={campaigns.error} onRetry={() => void campaigns.refetch()} />
        </Card>
      ) : !campaigns.data ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }, (_, i) => (
            <Skeleton key={i} className="h-44" />
          ))}
        </div>
      ) : campaigns.data.items.length === 0 ? (
        <Card>
          {status ? (
            <EmptyState icon={<Search />} title={`No ${STATUS_BADGE[status].label.toLowerCase()} campaigns`} description="Try another status." />
          ) : (
            <EmptyState
              icon={<Megaphone />}
              title="Plan your first campaign"
              description="Pick a published agent and an audience, set calling hours and pacing, then rehearse it as a dry run before going live."
              action={
                canManage ? (
                  <Button variant="primary" onClick={() => setCreating(true)}>
                    <Plus /> New campaign
                  </Button>
                ) : null
              }
            />
          )}
        </Card>
      ) : (
        <>
          <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {campaigns.data.items.map((c) => (
              <li key={c.id}>
                <CampaignCard campaign={c} agentName={c.agent_id ? names.get(c.agent_id) : undefined} />
              </li>
            ))}
          </ul>
          {campaigns.data.total > PAGE_SIZE ? (
            <Card className="mt-4">
              <Pagination page={page} pageSize={PAGE_SIZE} total={campaigns.data.total} onPageChange={(p) => set("page", String(p))} />
            </Card>
          ) : null}
        </>
      )}

      <CampaignWizard open={creating} onOpenChange={setCreating} />
    </>
  );
}

function CampaignCard({ campaign: c, agentName }: { campaign: Campaign; agentName: string | undefined }) {
  const status = STATUS_BADGE[c.status];
  const mode = MODE_BADGE[c.mode];
  const counts = leadCounts(undefined, c);
  const total = totalLeads(counts);
  const settled = settledLeads(counts);
  const stats = c.stats as { answer_rate?: number | null; calls?: { total?: number } };
  const started = c.started_at ?? c.start_at;

  return (
    <Card className="flex h-full flex-col transition-colors hover:border-line-strong">
      <Link to={`/campaigns/${c.id}`} className="flex flex-1 flex-col gap-3 p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="truncate text-[14px] font-semibold text-ink">{c.name}</h2>
            <p className="mt-0.5 flex items-center gap-1.5 truncate text-[12.5px] text-muted">
              <Bot className="size-3.5 shrink-0" aria-hidden />
              {agentName ?? "Agent"}
            </p>
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <Badge tone={status.tone}>{status.label}</Badge>
            <Badge tone={mode.tone} title={mode.description}>
              {mode.label}
            </Badge>
          </div>
        </div>

        {total > 0 ? (
          <div>
            <div className="flex items-baseline justify-between text-[12px] text-muted">
              <span>
                <span className="font-mono text-ink-2">{formatNumber(settled)}</span> of <span className="font-mono text-ink-2">{formatNumber(total)}</span> leads done
              </span>
              <span className="font-mono text-ink-2">{formatPercent(total ? settled / total : null)}</span>
            </div>
            <Tooltip content={LEAD_STATUS_ORDER.filter((s) => counts[s] > 0).map((s) => `${LEAD_STATUS[s].label} ${formatNumber(counts[s])}`).join(" · ")}>
              <div className="mt-1.5 flex h-2 w-full gap-px overflow-hidden rounded-full bg-surface-2" role="img" aria-label={`${settled} of ${total} leads done`}>
                {LEAD_STATUS_ORDER.map((s) => (counts[s] > 0 ? <span key={s} className={cn("h-full", LEAD_STATUS[s].bar)} style={{ width: `${(counts[s] / total) * 100}%` }} /> : null))}
              </div>
            </Tooltip>
          </div>
        ) : (
          <p className="text-[12px] text-muted">{c.status === "draft" ? "Audience is snapshotted when the campaign starts." : "No leads in the audience."}</p>
        )}

        <dl className="mt-auto grid grid-cols-3 gap-2 text-[12px]">
          <div>
            <dt className="text-muted">Calls</dt>
            <dd className="font-mono text-[13px] text-ink">{formatNumber(stats.calls?.total ?? 0)}</dd>
          </div>
          <div>
            <dt className="text-muted">Answer rate</dt>
            <dd className="font-mono text-[13px] text-ink">{formatPercent(stats.answer_rate ?? null)}</dd>
          </div>
          <div>
            <dt className="text-muted">{c.started_at ? "Started" : c.start_at ? "Starts" : "Created"}</dt>
            <dd className="text-[13px] text-ink" title={formatDateTime(started ?? c.created_at)}>
              {formatRelative(started ?? c.created_at)}
            </dd>
          </div>
        </dl>
      </Link>
    </Card>
  );
}
