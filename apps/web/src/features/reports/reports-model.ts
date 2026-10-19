/**
 * Pure helpers for the reports page: metric definitions (one place for label, meaning and
 * formatting, so tooltips and tables never disagree), null-safe rates, and the client-side
 * CSV export. Component-free.
 */
import { formatMs } from "@/features/calls/latency-model";
import type { CallsOverview } from "@/features/queries";
import { formatCost, formatDuration, formatNumber, formatPercent } from "@/lib/format";

export type Range = 7 | 30 | 90;
export const RANGES: Range[] = [7, 30, 90];

export interface Metric {
  id: string;
  label: string;
  /** The definition shown in the tooltip; mirrors the API field description. */
  definition: string;
  value: (o: CallsOverview) => string;
  /** `true` when the metric is undefined (null) rather than zero. */
  undefinedFor?: (o: CallsOverview) => boolean;
  /** The line under the value, when it is more specific than the default denominator. */
  note?: (o: CallsOverview) => string;
}

/** "Over 12 measured calls": response times only count calls whose turns were timed. */
export function measuredNote(o: CallsOverview): string {
  const n = o.measured_calls ?? 0;
  if (n === 0) return "No measured calls yet";
  return `Over ${formatNumber(n)} measured ${n === 1 ? "call" : "calls"}`;
}

/** Null means "not computable" (no denominator) and is shown as "—"; zero is a real zero. */
export const METRICS: Metric[] = [
  { id: "attempted", label: "Attempted", definition: "Calls that reached a terminal state: answered, no answer, busy, failed or canceled.", value: (o) => formatNumber(o.totals.attempted) },
  { id: "answered", label: "Answered", definition: "Calls the person picked up and that finished normally (state completed).", value: (o) => formatNumber(o.totals.answered) },
  {
    id: "answer_rate",
    label: "Answer rate",
    definition: "Answered ÷ attempted. Undefined until a call reaches a terminal state.",
    value: (o) => formatPercent(o.answer_rate),
    undefinedFor: (o) => o.answer_rate === null,
  },
  {
    id: "qualification_rate",
    label: "Qualified rate",
    definition: "Answered calls with the outcome “qualified” ÷ answered calls. Undefined until a call is answered.",
    value: (o) => formatPercent(o.qualification_rate),
    undefinedFor: (o) => o.qualification_rate === null,
  },
  {
    id: "avg_duration",
    label: "Avg. duration",
    definition: "Mean length of answered calls. Undefined until a call is answered.",
    value: (o) => formatDuration(o.avg_duration_seconds),
    undefinedFor: (o) => o.avg_duration_seconds === null,
  },
  {
    id: "estimated_cost",
    label: "Est. cost",
    definition: "Sum of the per-call cost estimates reported by the voice service. Undefined when no call reported one; simulated calls never do.",
    value: (o) => formatCost(o.estimated_cost),
    undefinedFor: (o) => o.estimated_cost === null,
  },
  {
    id: "response_p50",
    label: "Median response",
    definition: "Half of the agent's replies started faster than this, from the person going quiet to the agent speaking. Only calls with timed turns count; simulated timings never mix in.",
    value: (o) => formatMs(o.response_p50_ms),
    undefinedFor: (o) => o.response_p50_ms === null || o.response_p50_ms === undefined,
    note: measuredNote,
  },
  {
    id: "response_p95",
    label: "p95 response",
    definition: "19 out of 20 replies started faster than this: how slow the slower moments felt. Measured on the same calls as the median.",
    value: (o) => formatMs(o.response_p95_ms),
    undefinedFor: (o) => o.response_p95_ms === null || o.response_p95_ms === undefined,
    note: measuredNote,
  },
];

export function rate(numerator: number, denominator: number): number | null {
  return denominator > 0 ? numerator / denominator : null;
}

/* CSV */

function csvCell(v: string | number | null | undefined): string {
  if (v === null || v === undefined) return "";
  const s = String(v);
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

export function toCsv(headers: string[], rows: (string | number | null | undefined)[][]): string {
  return [headers, ...rows].map((r) => r.map(csvCell).join(",")).join("\r\n") + "\r\n";
}

/** The by-agent table as CSV. Rates are plain ratios (0–1) with 4 decimals; blanks mean undefined. */
export function byAgentCsv(rows: CallsOverview["by_agent"]): string {
  return toCsv(
    ["agent_id", "agent", "attempted", "answered", "answer_rate", "qualified", "qualification_rate", "avg_duration_seconds", "response_p50_ms"],
    rows.map((r) => {
      const ar = rate(r.answered, r.attempted);
      const qr = rate(r.qualified, r.answered);
      return [r.agent_id ?? "", r.agent_name ?? "No agent", r.attempted, r.answered, ar === null ? "" : ar.toFixed(4), r.qualified, qr === null ? "" : qr.toFixed(4), r.avg_duration_seconds ?? "", r.response_p50_ms ?? ""];
    }),
  );
}

export function downloadText(filename: string, text: string, type = "text/csv;charset=utf-8"): void {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = Object.assign(document.createElement("a"), { href: url, download: filename });
  a.click();
  URL.revokeObjectURL(url);
}

/* Daily chart */

export interface DailyPoint {
  date: string;
  answered: number;
  /** Attempted but not answered, so the two stack to the attempted total. */
  unanswered: number;
  attempted: number;
}

export function dailyPoints(daily: CallsOverview["daily"]): DailyPoint[] {
  return daily.map((d) => ({ date: d.date, answered: d.answered, unanswered: Math.max(0, d.attempted - d.answered), attempted: d.attempted }));
}
