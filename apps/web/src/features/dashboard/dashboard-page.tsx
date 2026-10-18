import { useQuery } from "@tanstack/react-query";
import { ArrowDownRight, ArrowUpRight, CheckSquare } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { EmptyState, ErrorState, PageHeader } from "@/components/common";
import { Card, CardHeader, Skeleton } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { qk, useCallsOverview, useOrgId, useTasks } from "@/features/queries";
import { api, unwrap, type Schemas } from "@/lib/api";
import { formatDate, formatNumber, formatPercent, formatRelative } from "@/lib/format";
import { useCurrentMembership } from "@/lib/session";
import { cn } from "@/lib/utils";

const RANGES = [7, 30, 90] as const;
type Overview = Schemas["CrmOverview"];

export default function DashboardPage() {
  const [range, setRange] = useState<(typeof RANGES)[number]>(30);
  const orgId = useOrgId();
  const org = useCurrentMembership()?.organization;
  const overview = useQuery({
    queryKey: qk.overview(orgId, range),
    queryFn: () => unwrap(api.GET("/api/v1/analytics/crm-overview", { params: { query: { range_days: range } } })),
    enabled: !!orgId,
  });

  return (
    <>
      <PageHeader
        eyebrow={org?.name}
        title="Overview"
        description="Pipeline health from live CRM data."
        actions={
          <Segmented
            label="Date range"
            value={String(range) as `${(typeof RANGES)[number]}`}
            onChange={(v) => setRange(Number(v) as (typeof RANGES)[number])}
            options={RANGES.map((r) => ({ value: String(r) as `${typeof r}`, label: `${r} days` }))}
          />
        }
      />
      {overview.isError ? (
        <Card>
          <ErrorState error={overview.error} onRetry={() => void overview.refetch()} />
        </Card>
      ) : (
        <div className="flex flex-col gap-5">
          <KpiStrip data={overview.data} />
          <div className="grid gap-5 lg:grid-cols-3">
            <Card className="lg:col-span-2">
              <CardHeader title="New leads per day" description={`Last ${range} days, in ${org?.timezone ?? "UTC"}`} />
              <div className="h-64 px-2 pb-2 pt-4">
                {overview.data ? <DailyChart data={overview.data.daily_new_leads} /> : <Skeleton className="m-2 h-56" />}
              </div>
            </Card>
            <PipelineCard data={overview.data} />
          </div>
          <div className="grid gap-5 lg:grid-cols-3">
            <SourcesCard data={overview.data} />
            <MyTasksCard />
            <DataHealthCard data={overview.data} />
          </div>
        </div>
      )}
    </>
  );
}

function KpiStrip({ data }: { data: Overview | undefined }) {
  const delta = data ? data.new_leads - data.new_leads_previous : 0;
  const cells: { label: string; value: string; note: React.ReactNode; to?: string }[] = data
    ? [
        { label: "Leads in CRM", value: formatNumber(data.total_leads), note: "All live leads", to: "/leads" },
        {
          label: `New · ${data.range_days}d`,
          value: formatNumber(data.new_leads),
          note: (
            <span className={cn("inline-flex items-center gap-0.5", delta > 0 && "text-success", delta < 0 && "text-danger")}>
              {delta > 0 ? <ArrowUpRight className="size-3.5" /> : delta < 0 ? <ArrowDownRight className="size-3.5" /> : null}
              {delta === 0 ? "Same as" : `${delta > 0 ? "+" : ""}${formatNumber(delta)} vs`} prior {data.range_days}d
            </span>
          ),
        },
        {
          label: "Converted",
          value: formatNumber(data.converted_leads),
          note: data.conversion_rate === null ? "No leads yet" : `${formatPercent(data.conversion_rate)} of all leads`,
        },
        { label: "Follow-ups due", value: formatNumber(data.follow_ups_due), note: "Follow-up time has passed", to: "/leads?sort=next_follow_up_at" },
        {
          label: "Overdue tasks",
          value: formatNumber(data.overdue_tasks),
          note: `${formatNumber(data.open_tasks)} open in total`,
          to: "/tasks",
        },
      ]
    : [];

  return (
    <Card className="grid grid-cols-2 divide-line sm:grid-cols-3 lg:grid-cols-6 lg:divide-x [&>*]:border-line">
      {data
        ? cells.map((c) => {
            const body = (
              <>
                <div className="eyebrow">{c.label}</div>
                <div className="mt-2 font-mono text-[28px] font-medium leading-none tracking-tight text-ink">{c.value}</div>
                <div className="mt-2 text-[12px] text-muted">{c.note}</div>
              </>
            );
            return c.to ? (
              <Link key={c.label} to={c.to} className="border-b px-5 py-4 transition-colors hover:bg-surface-2/60 lg:border-b-0">
                {body}
              </Link>
            ) : (
              <div key={c.label} className="border-b px-5 py-4 lg:border-b-0">
                {body}
              </div>
            );
          })
        : Array.from({ length: 5 }, (_, i) => (
            <div key={i} className="px-5 py-4">
              <Skeleton className="h-3 w-20" />
              <Skeleton className="mt-3 h-7 w-16" />
              <Skeleton className="mt-3 h-3 w-28" />
            </div>
          ))}
      <CallsCell />
    </Card>
  );
}

/** Answered over attempted calls in the last 30 days; the full picture lives in Reports. */
function CallsCell() {
  const calls = useCallsOverview({ range_days: 30 });
  if (!calls.data) {
    return (
      <div className="px-5 py-4">
        <Skeleton className="h-3 w-20" />
        <Skeleton className="mt-3 h-7 w-16" />
        <Skeleton className="mt-3 h-3 w-28" />
      </div>
    );
  }
  const t = calls.data.totals;
  return (
    <Link to="/reports" className="border-b px-5 py-4 transition-colors hover:bg-surface-2/60 lg:border-b-0">
      <div className="eyebrow">Calls · 30d</div>
      <div className="mt-2 font-mono text-[28px] font-medium leading-none tracking-tight text-ink">
        {formatNumber(t.answered)}
        <span className="text-[16px] text-faint">/{formatNumber(t.attempted)}</span>
      </div>
      <div className="mt-2 text-[12px] text-muted">{calls.data.answer_rate === null ? "No calls attempted yet" : `${formatPercent(calls.data.answer_rate)} answered`}</div>
    </Link>
  );
}

function DailyChart({ data }: { data: Overview["daily_new_leads"] }) {
  const max = Math.max(...data.map((d) => d.count));
  if (max === 0) {
    return <EmptyState title="No new leads in this range" description="Add leads or import a CSV to see daily volume." className="py-16" />;
  }
  return (
    <ResponsiveContainer width="100%" height="100%">
      <BarChart data={data} margin={{ top: 4, right: 12, bottom: 0, left: -12 }}>
        <CartesianGrid vertical={false} stroke="var(--line)" />
        <XAxis
          dataKey="date"
          tickLine={false}
          axisLine={false}
          tick={{ fill: "var(--faint)", fontSize: 11 }}
          tickFormatter={(d: string) => formatDate(`${d}T12:00:00`).replace(/,? \d{4}$/, "")}
          minTickGap={24}
        />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} tick={{ fill: "var(--faint)", fontSize: 11 }} width={40} />
        <Tooltip
          cursor={{ fill: "var(--surface-2)" }}
          contentStyle={{ background: "var(--surface)", border: "1px solid var(--line)", borderRadius: 8, fontSize: 12 }}
          labelFormatter={(d) => formatDate(`${String(d)}T12:00:00`)}
          formatter={(v) => [formatNumber(Number(v)), "New leads"]}
        />
        <Bar dataKey="count" fill="var(--accent)" radius={[3, 3, 0, 0]} maxBarSize={22} />
      </BarChart>
    </ResponsiveContainer>
  );
}

function PipelineCard({ data }: { data: Overview | undefined }) {
  const max = Math.max(1, ...(data?.by_stage.map((s) => s.count) ?? [1]));
  return (
    <Card>
      <CardHeader title="Pipeline" description="Live leads by current stage" />
      <ul className="flex flex-col gap-2.5 px-4 py-4">
        {data
          ? data.by_stage.map((s) => (
              <li key={s.stage_id}>
                <Link to={`/leads?stage=${s.stage_id}`} className="group block">
                  <div className="flex items-baseline justify-between text-[12.5px]">
                    <span className={cn("font-medium text-ink-2 group-hover:text-ink", s.category === "lost" && "text-muted")}>{s.name}</span>
                    <span className="font-mono text-ink-2">{formatNumber(s.count)}</span>
                  </div>
                  <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-surface-2">
                    <div
                      className={cn("h-full rounded-full", s.category === "won" ? "bg-success" : s.category === "lost" ? "bg-lost" : "bg-accent")}
                      style={{ width: `${(s.count / max) * 100}%` }}
                    />
                  </div>
                </Link>
              </li>
            ))
          : Array.from({ length: 6 }, (_, i) => <Skeleton key={i} className="h-7" />)}
      </ul>
    </Card>
  );
}

function SourcesCard({ data }: { data: Overview | undefined }) {
  const total = data?.by_source.reduce((a, s) => a + s.count, 0) ?? 0;
  return (
    <Card>
      <CardHeader title="Lead sources" description="Where live leads came from" />
      {data && data.by_source.length === 0 ? (
        <EmptyState title="No leads yet" className="py-10" />
      ) : (
        <ul className="divide-y divide-line">
          {(data?.by_source ?? []).map((s) => (
            <li key={s.source ?? "none"} className="flex items-center justify-between px-4 py-2.5 text-[13px]">
              <span className={cn("font-mono text-[12.5px]", !s.source && "text-muted")}>{s.source ?? "not set"}</span>
              <span className="flex items-center gap-3">
                <span className="text-[12px] text-muted">{formatPercent(total ? s.count / total : null)}</span>
                <span className="w-10 text-right font-mono">{formatNumber(s.count)}</span>
              </span>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function MyTasksCard() {
  const tasks = useTasks({ mine: true, status: "open", page_size: 6 });
  const now = Date.now();
  return (
    <Card>
      <CardHeader
        title="Your open tasks"
        action={
          <Link to="/tasks" className="text-[12.5px] font-medium text-accent hover:underline">
            View all
          </Link>
        }
      />
      {tasks.data && tasks.data.items.length === 0 ? (
        <EmptyState icon={<CheckSquare />} title="Nothing on your list" description="Tasks assigned to you will appear here." className="py-10" />
      ) : (
        <ul className="divide-y divide-line">
          {(tasks.data?.items ?? []).map((t) => {
            const overdue = t.due_at ? new Date(t.due_at).getTime() < now : false;
            return (
              <li key={t.id} className="flex items-center justify-between gap-3 px-4 py-2.5 text-[13px]">
                {t.lead_id ? (
                  <Link to={`/leads/${t.lead_id}`} className="min-w-0 truncate font-medium hover:text-accent">
                    {t.title}
                  </Link>
                ) : (
                  <span className="min-w-0 truncate font-medium">{t.title}</span>
                )}
                <span className={cn("shrink-0 text-[12px]", overdue ? "font-medium text-danger" : "text-muted")}>
                  {t.due_at ? formatRelative(t.due_at) : "No due date"}
                </span>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function DataHealthCard({ data }: { data: Overview | undefined }) {
  const rows = data
    ? [
        { label: "Without an owner", value: data.unassigned, to: "/leads?owner=unassigned" },
        { label: "Marked do-not-call", value: data.do_not_call, to: "/leads?dnc=true" },
        { label: "Follow-up overdue", value: data.follow_ups_due, to: "/leads?sort=next_follow_up_at" },
      ]
    : [];
  return (
    <Card>
      <CardHeader title="Needs attention" description="Leads that can't progress as they are" />
      <ul className="divide-y divide-line">
        {rows.map((r) => (
          <li key={r.label}>
            <Link to={r.to} className="flex items-center justify-between px-4 py-2.5 text-[13px] transition-colors hover:bg-surface-2/60">
              <span className="text-ink-2">{r.label}</span>
              <span className={cn("font-mono", r.value > 0 ? "text-ink" : "text-faint")}>{formatNumber(r.value)}</span>
            </Link>
          </li>
        ))}
      </ul>
    </Card>
  );
}
