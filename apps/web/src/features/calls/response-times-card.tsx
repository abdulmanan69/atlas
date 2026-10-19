/**
 * "Response times" on the call detail page: how long the person waited for each reply.
 * The summary comes with the call; the per-turn waterfall is fetched only when opened.
 * Drawn with plain elements so it follows the theme tokens without a chart library.
 */
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, Gauge, Info } from "lucide-react";
import { useState } from "react";

import { EmptyState, ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { Badge, Card, CardHeader, Skeleton, Tooltip } from "@/components/ui/primitives";
import {
  DEFAULT_THRESHOLDS,
  formatMs,
  isSlow,
  pct,
  SEGMENT_LEGEND,
  summaryItems,
  turnSegments,
  waterfallScale,
  withThresholds,
  type SegmentKind,
  type SummaryTone,
} from "@/features/calls/latency-model";
import type { LatencySummary, LatencyThresholds, LatencyTurn } from "@/features/calls/latency-types";
import { qk, useOrgId } from "@/features/queries";
import { api, ApiError, unwrap } from "@/lib/api";
import { cn } from "@/lib/utils";

const SEGMENT_CLASS: Record<SegmentKind, string> = {
  heard: "bg-accent/45",
  thinking: "bg-accent",
  tool: "bg-warn/80",
  speaking: "bg-success/75",
  other: "bg-line-strong",
};

const TONE_CLASS: Record<SummaryTone, string> = {
  good: "text-success",
  warn: "text-warn",
  bad: "text-danger",
  neutral: "text-ink",
};

export function ResponseTimesCard({ callId, summary, simulated }: { callId: string; summary: LatencySummary | null; simulated: boolean }) {
  const [open, setOpen] = useState(false);
  const synthetic = simulated || summary?.source === "simulated";
  return (
    <Card>
      <CardHeader
        title="Response times"
        description={synthetic ? "Synthetic timings from the dry run" : "How long the person waited for each reply"}
        action={summary && summary.slow_turns > 0 ? <Badge tone="warn">{summary.slow_turns === 1 ? "1 slow reply" : `${summary.slow_turns} slow replies`}</Badge> : null}
      />
      {!summary ? (
        <NotMeasured />
      ) : (
        <>
          <SummaryGrid summary={summary} />
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line px-4 py-2.5">
            <p className="text-[12px] text-muted">
              Measured on {summary.measured_turns} of {summary.turns} {summary.turns === 1 ? "turn" : "turns"}. Aim for under {formatMs(DEFAULT_THRESHOLDS.target_ms)}; over {formatMs(DEFAULT_THRESHOLDS.slow_ms)} feels slow.
            </p>
            <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
              {open ? "Hide each turn" : "Show each turn"}
              <ChevronDown className={cn("transition-transform", open && "rotate-180")} />
            </Button>
          </div>
          {open ? <Waterfall callId={callId} synthetic={synthetic} /> : null}
        </>
      )}
    </Card>
  );
}

function NotMeasured() {
  return <EmptyState icon={<Gauge />} title="Not measured for this call" description="Timings are recorded for answered calls once the voice service reports them." className="py-10" />;
}

function SummaryGrid({ summary }: { summary: LatencySummary }) {
  return (
    <dl className="grid grid-cols-2 gap-px bg-line sm:grid-cols-3" aria-label="Response time summary">
      {summaryItems(summary).map((item) => (
        <div key={item.id} className="bg-surface px-4 py-3">
          <dt>
            <Tooltip content={item.definition}>
              <span className="eyebrow inline-flex cursor-help items-center gap-1">
                {item.label}
                <Info className="size-3 text-faint" aria-hidden />
              </span>
            </Tooltip>
          </dt>
          <dd className={cn("mt-1.5 font-mono text-[20px] font-medium leading-none tracking-tight", item.value === "—" ? "text-faint" : TONE_CLASS[item.tone])}>{item.value}</dd>
          <dd className="sr-only">{item.definition}</dd>
        </div>
      ))}
    </dl>
  );
}

function Waterfall({ callId, synthetic }: { callId: string; synthetic: boolean }) {
  const o = useOrgId();
  const detail = useQuery({
    queryKey: qk.callLatency(o, callId),
    queryFn: () => unwrap(api.GET("/api/v1/calls/{call_id}/latency", { params: { path: { call_id: callId } } })),
    enabled: !!o,
    staleTime: 5 * 60_000,
    retry: (count, e) => !(e instanceof ApiError && e.status === 404) && count < 2,
  });

  if (detail.isError) {
    if (detail.error instanceof ApiError && detail.error.status === 404) return <div className="border-t border-line"><NotMeasured /></div>;
    return <ErrorState error={detail.error} onRetry={() => void detail.refetch()} className="border-t border-line" />;
  }
  if (!detail.data) {
    return (
      <div className="flex flex-col gap-2 border-t border-line p-4" aria-busy>
        {Array.from({ length: 4 }, (_, i) => (
          <Skeleton key={i} className="h-8" />
        ))}
      </div>
    );
  }
  const { turns, thresholds } = detail.data;
  if (turns.length === 0) return <p className="border-t border-line px-4 py-5 text-[13px] text-muted">No turns were recorded.</p>;
  return <TurnsChart turns={turns} thresholds={withThresholds(thresholds)} synthetic={synthetic} />;
}

function TurnsChart({ turns, thresholds, synthetic }: { turns: LatencyTurn[]; thresholds: LatencyThresholds; synthetic: boolean }) {
  const scale = waterfallScale(turns, thresholds);
  const marks = [
    { ms: thresholds.target_ms, label: "Target", className: "border-success/60" },
    { ms: thresholds.slow_ms, label: "Slow", className: "border-danger/50" },
  ];
  return (
    <div className="border-t border-line">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1.5 px-4 pt-3 text-[11.5px] text-muted" aria-label="Legend">
        {SEGMENT_LEGEND.map((l) => (
          <Tooltip key={l.kind} content={l.definition}>
            <span className="inline-flex cursor-help items-center gap-1.5">
              <span className={cn("size-2.5 rounded-[2px]", SEGMENT_CLASS[l.kind])} aria-hidden />
              {l.label}
            </span>
          </Tooltip>
        ))}
        <span className="ml-auto text-faint">{synthetic ? "Synthetic timings from the dry run" : "Approximate breakdown; the total is measured"}</span>
      </div>

      <div className="grid grid-cols-[minmax(0,140px)_minmax(0,1fr)_72px] gap-x-3 px-4 pb-1 pt-3 text-[10.5px] uppercase tracking-wider text-faint">
        <span>Turn</span>
        <div className="relative h-4" aria-hidden>
          <span className="absolute left-0">0</span>
          {marks.map((m) => (
            <span key={m.label} className="absolute -translate-x-1/2 whitespace-nowrap" style={{ left: `${pct(m.ms, scale)}%` }}>
              {formatMs(m.ms)}
            </span>
          ))}
          <span className="absolute right-0">{formatMs(scale)}</span>
        </div>
        <span className="text-right">Response</span>
      </div>

      <ol aria-label="Turns" className="flex flex-col pb-2">
        {turns.map((t) => {
          const segments = turnSegments(t);
          const slow = isSlow(t, thresholds);
          return (
            <li
              key={t.turn}
              data-slow={slow || undefined}
              className={cn("grid grid-cols-[minmax(0,140px)_minmax(0,1fr)_72px] items-center gap-x-3 px-4 py-1.5", slow && "bg-danger-soft/50")}
            >
              <div className="min-w-0">
                <p className="truncate text-[12.5px] font-medium text-ink">
                  <span className="font-mono text-faint">{t.turn}</span> {t.node_name ?? "Turn"}
                </p>
                {t.user_text ? (
                  <p className="truncate text-[11.5px] text-muted" title={t.user_text}>
                    “{t.user_text}”
                  </p>
                ) : null}
              </div>
              <div className="relative h-5 rounded-[3px] bg-surface-2">
                {marks.map((m) => (
                  <span key={m.label} aria-hidden className={cn("absolute inset-y-0 border-l border-dashed", m.className)} style={{ left: `${pct(m.ms, scale)}%` }} />
                ))}
                {segments.map((s, i) => (
                  <Tooltip key={i} content={`${s.label}: ${formatMs(s.ms)}`}>
                    <span
                      data-kind={s.kind}
                      aria-label={`${s.label} ${formatMs(s.ms)}`}
                      className={cn("absolute inset-y-1 rounded-[2px] ring-1 ring-surface", SEGMENT_CLASS[s.kind])}
                      style={{ left: `${pct(s.offset, scale)}%`, width: `max(2px, ${pct(s.ms, scale)}%)` }}
                    />
                  </Tooltip>
                ))}
              </div>
              <div className="flex items-center justify-end gap-1.5">
                <span className={cn("font-mono text-[12.5px]", t.response_ms === null ? "text-faint" : slow ? "font-semibold text-danger" : "text-ink")}>{formatMs(t.response_ms)}</span>
                {slow ? <span className="sr-only">, slow</span> : null}
              </div>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
