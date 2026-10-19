import { Download, Info, PhoneOff } from "lucide-react";
import { useMemo } from "react";
import { Link, useSearchParams } from "react-router";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip as ChartTooltip, XAxis, YAxis } from "recharts";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, Skeleton, Tooltip } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { Select, SelectItem } from "@/components/ui/select";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { MODE_BADGE, outcomeLabel, outcomeTone, type ModeChoice } from "@/features/calls/calls-model";
import { formatMs, responseTone } from "@/features/calls/latency-model";
import { useAgents, useCallsOverview, useCampaigns, type CallsOverview } from "@/features/queries";
import { byAgentCsv, dailyPoints, downloadText, METRICS, RANGES, rate, type Range } from "@/features/reports/reports-model";
import { formatDate, formatDuration, formatNumber, formatPercent } from "@/lib/format";
import { useCurrentMembership } from "@/lib/session";
import { cn } from "@/lib/utils";

export default function ReportsPage() {
  const [params, setParams] = useSearchParams();
  const org = useCurrentMembership()?.organization;
  const range = ((RANGES as number[]).includes(Number(params.get("range"))) ? Number(params.get("range")) : 30) as Range;
  const agent = params.get("agent") || null;
  const campaign = params.get("campaign") || null;
  const modeParam = params.get("mode");
  const mode: ModeChoice = modeParam === "live" || modeParam === "simulated" ? modeParam : "all";

  const update = (patch: Record<string, string | null>) => {
    const next = new URLSearchParams(params);
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "" || (k === "mode" && v === "all") || (k === "range" && v === "30")) next.delete(k);
      else next.set(k, v);
    }
    setParams(next, { replace: true });
  };

  const query = useMemo(
    () => ({ range_days: range, agent_id: agent ?? undefined, campaign_id: campaign ?? undefined, mode: mode === "all" ? undefined : mode }),
    [range, agent, campaign, mode],
  );
  const overview = useCallsOverview(query);
  const agents = useAgents(true);
  const campaigns = useCampaigns({ page_size: 100 });
  const agentOptions = useMemo(() => (agents.data ?? []).filter((a) => a.status !== "archived" || a.id === agent), [agents.data, agent]);
  const data = overview.data;

  return (
    <>
      <PageHeader
        eyebrow={org?.name}
        title="Reports"
        description="What your agents attempted, who answered, what came of it, and what it cost."
        actions={
          <Segmented
            label="Date range"
            value={String(range) as `${Range}`}
            onChange={(v) => update({ range: v })}
            options={RANGES.map((r) => ({ value: String(r) as `${Range}`, label: `${r} days` }))}
          />
        }
      />
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <Select aria-label="Agent" value={agent ?? ""} onValueChange={(v) => update({ agent: v })} allowEmpty emptyLabel="All agents" className="w-auto min-w-40">
          {agentOptions.map((a) => (
            <SelectItem key={a.id} value={a.id} description={a.status === "archived" ? "Archived" : undefined}>
              {a.name}
            </SelectItem>
          ))}
        </Select>
        <Select aria-label="Campaign" value={campaign ?? ""} onValueChange={(v) => update({ campaign: v })} allowEmpty emptyLabel="All campaigns" className="w-auto min-w-40">
          {(campaigns.data?.items ?? []).map((c) => (
            <SelectItem key={c.id} value={c.id}>
              {c.name}
            </SelectItem>
          ))}
        </Select>
        <Segmented<ModeChoice>
          label="Mode"
          value={mode}
          onChange={(v) => update({ mode: v })}
          options={[
            { value: "all", label: "All calls" },
            { value: "live", label: "Live" },
            { value: "simulated", label: "Simulated" },
          ]}
        />
        {data && data.mode_split.simulated > 0 && mode === "all" ? (
          <span className="inline-flex items-center gap-1 text-[12px] text-muted">
            <Info className="size-3.5" aria-hidden /> Includes {formatNumber(data.mode_split.simulated)} simulated {data.mode_split.simulated === 1 ? "call" : "calls"}
          </span>
        ) : null}
      </div>

      {overview.isError ? (
        <Card>
          <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />
        </Card>
      ) : (
        <div className={cn("flex flex-col gap-5", overview.isPlaceholderData && "opacity-70 transition-opacity")}>
          <KpiStrip data={data} />
          <div className="grid gap-5 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="Attempted vs answered per day" description={`Last ${range} days, in ${org?.timezone ?? "UTC"}. Answered calls are a part of attempted calls.`} />
              <div className="h-64 px-2 pb-2 pt-4">{data ? <DailyChart data={data} /> : <Skeleton className="m-2 h-56" />}</div>
            </Card>
            <OutcomesCard data={data} />
          </div>
          <div className="grid gap-5 lg:grid-cols-3">
            <AgentsCard data={data} className="lg:col-span-2" />
            <ModeCard data={data} />
          </div>
          <CampaignsCard data={data} />
        </div>
      )}
    </>
  );
}

function KpiStrip({ data }: { data: CallsOverview | undefined }) {
  return (
    <Card className="grid grid-cols-2 divide-line sm:grid-cols-4 xl:grid-cols-8 xl:divide-x [&>*]:border-line">
      {data
        ? METRICS.map((m) => {
            const undefinedValue = m.undefinedFor?.(data) ?? false;
            return (
              <div key={m.id} className="border-b px-5 py-4 xl:border-b-0">
                <Tooltip content={m.definition}>
                  <div className="eyebrow inline-flex cursor-help items-center gap-1">
                    {m.label}
                    <Info className="size-3 text-faint" aria-hidden />
                  </div>
                </Tooltip>
                <div className={cn("mt-2 font-mono text-[28px] font-medium leading-none tracking-tight", undefinedValue ? "text-faint" : "text-ink")}>{m.value(data)}</div>
                <div className="mt-2 text-[12px] text-muted">{m.note ? m.note(data) : undefinedValue ? "Not yet measurable" : m.id === "attempted" ? `${formatNumber(data.totals.in_progress)} still in progress` : m.id === "answered" ? `${formatNumber(data.totals.qualified)} qualified` : m.id === "estimated_cost" ? "Reported by the voice service" : `Over ${formatNumber(data.totals.attempted)} attempted`}</div>
              </div>
            );
          })
        : Array.from({ length: METRICS.length }, (_, i) => (
            <div key={i} className="px-5 py-4">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="mt-3 h-7 w-16" />
              <Skeleton className="mt-3 h-3 w-28" />
            </div>
          ))}
    </Card>
  );
}

function DailyChart({ data }: { data: CallsOverview }) {
  const points = dailyPoints(data.daily);
  if (data.totals.attempted === 0) {
    return <EmptyState icon={<PhoneOff />} title="No calls attempted in this range" description="Start a call or launch a campaign to see daily volume." className="py-12" />;
  }
  const day = (d: string) => formatDate(`${d}T12:00:00`).replace(/,? \d{4}$/, "");
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={points} margin={{ top: 4, right: 12, bottom: 0, left: -12 }} barCategoryGap="25%">
        <CartesianGrid vertical={false} stroke="var(--line)" />
        <XAxis dataKey="date" tickLine={false} axisLine={false} tick={{ fill: "var(--faint)", fontSize: 11 }} tickFormatter={day} minTickGap={24} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: "var(--faint)", fontSize: 11 }} width={40} />
        <ChartTooltip
          cursor={{ fill: "var(--surface-2)" }}
          contentStyle={{ background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 8, fontSize: 12, color: "var(--ink)" }}
          labelFormatter={(d) => formatDate(`${String(d)}T12:00:00`)}
          formatter={(v, name) => [formatNumber(Number(v)), name === "answered" ? "Answered" : "Not answered"]}
          itemSorter={(item) => (item.dataKey === "answered" ? 0 : 1)}
        />
        <Legend
          verticalAlign="top"
          align="right"
          height={24}
          iconType="square"
          iconSize={8}
          formatter={(value: string) => <span style={{ color: "var(--muted)", fontSize: 11.5 }}>{value === "answered" ? "Answered" : "Attempted, not answered"}</span>}
        />
        <Bar dataKey="answered" stackId="calls" fill="var(--accent)" maxBarSize={22} />
        <Bar dataKey="unanswered" stackId="calls" fill="var(--line-strong)" radius={[3, 3, 0, 0]} maxBarSize={22} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function OutcomesCard({ data }: { data: CallsOverview | undefined }) {
  const max = Math.max(1, ...(data?.outcomes.map((o) => o.count) ?? [1]));
  const total = data?.totals.answered ?? 0;
  return (
    <Card>
      <CardHeader title="Outcomes" description="Answered calls by the agent's outcome code" />
      {data && data.outcomes.length === 0 ? (
        <EmptyState title="No answered calls yet" className="py-10" />
      ) : (
        <ul className="flex flex-col gap-2.5 px-4 py-4">
          {data
            ? data.outcomes.map((o) => (
                <li key={o.outcome ?? "none"}>
                  <div className="flex items-baseline justify-between gap-3 text-[12.5px]">
                    <span className="flex min-w-0 items-center gap-2">
                      {o.outcome ? <Badge tone={outcomeTone(o.outcome)}>{outcomeLabel(o.outcome)}</Badge> : <span className="text-muted">No outcome recorded</span>}
                      <Link to={`/calls?state=completed${o.outcome ? `&outcome=${encodeURIComponent(o.outcome)}` : ""}`} className="truncate text-[12px] text-faint hover:text-accent">
                        view calls
                      </Link>
                    </span>
                    <span className="flex shrink-0 items-center gap-3">
                      <span className="text-[12px] text-muted">{formatPercent(rate(o.count, total))}</span>
                      <span className="w-10 text-right font-mono text-ink-2">{formatNumber(o.count)}</span>
                    </span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
                    <div className={cn("h-full rounded-full", outcomeTone(o.outcome) === "success" ? "bg-success" : outcomeTone(o.outcome) === "danger" ? "bg-danger" : "bg-accent")} style={{ width: `${(o.count / max) * 100}%` }} />
                  </div>
                </li>
              ))
            : Array.from({ length: 5 }, (_, i) => <Skeleton key={i} className="h-7" />)}
        </ul>
      )}
    </Card>
  );
}

function AgentsCard({ data, className }: { data: CallsOverview | undefined; className?: string }) {
  return (
    <Card className={cn("overflow-hidden", className)}>
      <CardHeader
        title="By agent"
        description="Most attempted first"
        action={
          <Button size="sm" variant="ghost" disabled={!data || data.by_agent.length === 0} onClick={() => data && downloadText(`calls-by-agent-${data.range_days}d-${new Date().toISOString().slice(0, 10)}.csv`, byAgentCsv(data.by_agent))}>
            <Download /> Export CSV
          </Button>
        }
      />
      {data && data.by_agent.length === 0 ? (
        <EmptyState title="No calls in this range" className="py-10" />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Agent</Th>
              <Th className="text-right">Attempted</Th>
              <Th className="text-right">Answered</Th>
              <Th className="text-right">Answer rate</Th>
              <Th className="text-right">Qualified</Th>
              <Th className="text-right">Avg. duration</Th>
              <Th className="text-right">
                <Tooltip content="Median time from the person going quiet to the agent speaking, over this agent's measured calls.">
                  <span className="cursor-help">Response</span>
                </Tooltip>
              </Th>
            </tr>
          </thead>
          <tbody>
            {data
              ? data.by_agent.map((r) => (
                  <Tr key={r.agent_id ?? "none"}>
                    <Td>
                      {r.agent_id ? (
                        <Link to={`/agents/${r.agent_id}`} className="font-semibold hover:text-accent">
                          {r.agent_name ?? "Agent"}
                        </Link>
                      ) : (
                        <span className="text-muted">No agent (unknown inbound)</span>
                      )}
                    </Td>
                    <Td className="text-right font-mono">{formatNumber(r.attempted)}</Td>
                    <Td className="text-right font-mono">{formatNumber(r.answered)}</Td>
                    <Td className="text-right font-mono">{formatPercent(rate(r.answered, r.attempted))}</Td>
                    <Td className="text-right font-mono">{formatNumber(r.qualified)}</Td>
                    <Td className={cn("text-right font-mono", r.avg_duration_seconds === null && "text-faint")}>{formatDuration(r.avg_duration_seconds)}</Td>
                    <Td className={cn("text-right font-mono", RESPONSE_TONE[responseTone(r.response_p50_ms)])}>{formatMs(r.response_p50_ms)}</Td>
                  </Tr>
                ))
              : Array.from({ length: 3 }, (_, i) => (
                  <tr key={i}>
                    <Td colSpan={7}>
                      <Skeleton className="h-7" />
                    </Td>
                  </tr>
                ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}

const RESPONSE_TONE = { good: "text-success", warn: "text-warn", bad: "text-danger", neutral: "text-faint" } as const;

function ModeCard({ data }: { data: CallsOverview | undefined }) {
  const split = data?.mode_split;
  // Browser test calls are never part of the report; they are only mentioned below.
  const total = split ? split.live + split.simulated : 0;
  const rows: { key: "live" | "simulated"; bar: string }[] = [
    { key: "live", bar: "bg-success" },
    { key: "simulated", bar: "bg-line-strong" },
  ];
  return (
    <Card>
      <CardHeader title="Live vs simulated" description="All calls in the range, by mode" />
      <ul className="flex flex-col gap-3 px-4 py-4">
        {split
          ? rows.map((r) => (
              <li key={r.key}>
                <div className="flex items-baseline justify-between text-[12.5px]">
                  <span className="flex items-center gap-2">
                    <Badge tone={MODE_BADGE[r.key].tone}>{MODE_BADGE[r.key].label}</Badge>
                    <span className="text-[12px] text-muted">{formatPercent(rate(split[r.key], total))}</span>
                  </span>
                  <span className="font-mono text-ink-2">{formatNumber(split[r.key])}</span>
                </div>
                <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
                  <div className={cn("h-full rounded-full", r.bar)} style={{ width: `${total ? (split[r.key] / total) * 100 : 0}%` }} />
                </div>
              </li>
            ))
          : Array.from({ length: 2 }, (_, i) => <Skeleton key={i} className="h-7" />)}
        {split ? <li className="text-[12px] text-muted">{MODE_BADGE.simulated.description}</li> : null}
        {split && split.test > 0 ? (
          <li className="text-[12px] text-muted">
            {formatNumber(split.test)} browser test {split.test === 1 ? "call" : "calls"} from the agent editor {split.test === 1 ? "is" : "are"} left out of every figure on this page.
          </li>
        ) : null}
      </ul>
    </Card>
  );
}

function CampaignsCard({ data }: { data: CallsOverview | undefined }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader title="By campaign" description="Calls placed by campaigns in this range, most attempted first" />
      {data && data.by_campaign.length === 0 ? (
        <EmptyState title="No campaign calls in this range" className="py-10" />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Campaign</Th>
              <Th className="text-right">Attempted</Th>
              <Th className="text-right">Answered</Th>
              <Th className="text-right">Answer rate</Th>
              <Th className="text-right">Qualified</Th>
              <Th className="text-right">Qualified rate</Th>
            </tr>
          </thead>
          <tbody>
            {data
              ? data.by_campaign.map((r) => (
                  <Tr key={r.campaign_id}>
                    <Td>
                      <Link to={`/campaigns/${r.campaign_id}`} className="font-semibold hover:text-accent">
                        {r.name}
                      </Link>
                    </Td>
                    <Td className="text-right font-mono">{formatNumber(r.attempted)}</Td>
                    <Td className="text-right font-mono">{formatNumber(r.answered)}</Td>
                    <Td className="text-right font-mono">{formatPercent(rate(r.answered, r.attempted))}</Td>
                    <Td className="text-right font-mono">{formatNumber(r.qualified)}</Td>
                    <Td className="text-right font-mono">{formatPercent(rate(r.qualified, r.answered))}</Td>
                  </Tr>
                ))
              : Array.from({ length: 2 }, (_, i) => (
                  <tr key={i}>
                    <Td colSpan={6}>
                      <Skeleton className="h-7" />
                    </Td>
                  </tr>
                ))}
          </tbody>
        </Table>
      )}
    </Card>
  );
}
