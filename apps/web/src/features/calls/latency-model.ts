/**
 * Pure helpers for the call's "Response times" card: millisecond formatting, the summary
 * tiles with their plain-language definitions, and the per-turn waterfall geometry.
 * Component-free so it is unit-testable.
 */
import type { LatencySummary, LatencyThresholds, LatencyTurn } from "@/features/calls/latency-types";
import type { Schemas } from "@/lib/api";

export const DEFAULT_THRESHOLDS: LatencyThresholds = { target_ms: 800, slow_ms: 1500 };

/** The API may omit either threshold; fill in the contract defaults. */
export function withThresholds(t: Schemas["LatencyThresholds"] | null | undefined): LatencyThresholds {
  return { target_ms: t?.target_ms ?? DEFAULT_THRESHOLDS.target_ms, slow_ms: t?.slow_ms ?? DEFAULT_THRESHOLDS.slow_ms };
}

/** `420 ms`, `1.2 s`, `14 s`; `null` = not measured, which is different from 0 ms. */
export function formatMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return "—";
  const v = Math.max(0, ms);
  if (v < 1000) return `${Math.round(v)} ms`;
  if (v < 10_000) return `${(Math.round(v / 100) / 10).toFixed(1)} s`;
  return `${Math.round(v / 1000)} s`;
}

export type SummaryTone = "good" | "warn" | "bad" | "neutral";

/** Colour for a response time against the targets: under target is good, over slow is bad. */
export function responseTone(ms: number | null | undefined, t: LatencyThresholds = DEFAULT_THRESHOLDS): SummaryTone {
  if (ms === null || ms === undefined) return "neutral";
  if (ms <= t.target_ms) return "good";
  if (ms < t.slow_ms) return "warn";
  return "bad";
}

export interface SummaryItem {
  id: string;
  label: string;
  value: string;
  definition: string;
  tone: SummaryTone;
}

export function summaryItems(s: LatencySummary, t: LatencyThresholds = DEFAULT_THRESHOLDS): SummaryItem[] {
  return [
    {
      id: "median",
      label: "Median response",
      value: formatMs(s.response_p50_ms),
      definition: "Half of the agent's replies started faster than this. Measured from when the person stopped talking to when the agent started speaking.",
      tone: responseTone(s.response_p50_ms, t),
    },
    {
      id: "p95",
      label: "p95 response",
      value: formatMs(s.response_p95_ms),
      definition: "19 out of 20 replies started faster than this. It shows how slow the slower moments felt.",
      tone: responseTone(s.response_p95_ms, t),
    },
    {
      id: "max",
      label: "Slowest",
      value: formatMs(s.response_max_ms),
      definition: "The longest wait for a reply in this call.",
      tone: responseTone(s.response_max_ms, t),
    },
    {
      id: "first",
      label: "First response",
      value: formatMs(s.first_response_ms),
      definition: "How long the agent took to answer the first time the person spoke.",
      tone: responseTone(s.first_response_ms, t),
    },
    {
      id: "interruptions",
      label: "Interruptions",
      value: String(s.interruptions),
      definition: "Times the person spoke over the agent. Many interruptions can mean replies are too long or start too late.",
      tone: "neutral",
    },
    {
      id: "tools",
      label: "Tool time",
      value: formatMs(s.tool_time_ms),
      definition: "Total time spent on knowledge lookups and actions while the person waited.",
      tone: "neutral",
    },
  ];
}

/* Waterfall */

export type SegmentKind = "heard" | "thinking" | "tool" | "speaking" | "other";

export interface Segment {
  kind: SegmentKind;
  label: string;
  ms: number;
  /** Start, in ms after the person stopped talking. */
  offset: number;
}

export const SEGMENT_LEGEND: { kind: SegmentKind; label: string; definition: string }[] = [
  { kind: "heard", label: "Heard", definition: "Speech recognition finishing the person's words" },
  { kind: "thinking", label: "Thinking", definition: "The language model producing its first words" },
  { kind: "tool", label: "Tools", definition: "Knowledge lookups and actions" },
  { kind: "speaking", label: "Speaking", definition: "The voice producing its first audio" },
  { kind: "other", label: "Other", definition: "Network, audio buffering and hand-offs between steps" },
];

const positive = (v: number | null | undefined): v is number => typeof v === "number" && Number.isFinite(v) && v > 0;

/**
 * One turn's wait, laid end to end: heard → thinking → tools → speaking, then whatever the
 * measured response time leaves unexplained as "other". Stages overlap a little in practice
 * (streaming), so this is an approximate breakdown; the response time is the measured truth.
 */
export function turnSegments(turn: LatencyTurn): Segment[] {
  const parts: Omit<Segment, "offset">[] = [];
  if (positive(turn.stt_ttfb_ms)) parts.push({ kind: "heard", label: "Heard", ms: turn.stt_ttfb_ms });
  if (positive(turn.llm_ttfb_ms)) parts.push({ kind: "thinking", label: "Thinking", ms: turn.llm_ttfb_ms });
  for (const tool of turn.tools ?? []) {
    if (positive(tool.duration_ms)) parts.push({ kind: "tool", label: tool.kind === "knowledge" ? "Knowledge lookup" : tool.name, ms: tool.duration_ms });
  }
  if (positive(turn.tts_ttfb_ms)) parts.push({ kind: "speaking", label: "Speaking", ms: turn.tts_ttfb_ms });
  const explained = parts.reduce((a, p) => a + p.ms, 0);
  if (positive(turn.response_ms) && turn.response_ms - explained > 0) parts.push({ kind: "other", label: "Other", ms: turn.response_ms - explained });
  let offset = 0;
  return parts.map((p) => {
    const s = { ...p, offset };
    offset += p.ms;
    return s;
  });
}

/** How far a turn's bar reaches: the larger of the measured response and its segments. */
export function turnExtent(turn: LatencyTurn): number {
  const segs = turnSegments(turn);
  const last = segs[segs.length - 1];
  return Math.max(turn.response_ms ?? 0, last ? last.offset + last.ms : 0);
}

/** One shared scale for every row, so bars compare; at least the slow threshold, rounded up to 500 ms. */
export function waterfallScale(turns: LatencyTurn[], t: LatencyThresholds = DEFAULT_THRESHOLDS): number {
  const max = Math.max(t.slow_ms, ...turns.map(turnExtent));
  return Math.ceil(max / 500) * 500;
}

/** Position as a percentage of the scale, clamped to 0–100. */
export function pct(ms: number, scale: number): number {
  if (scale <= 0) return 0;
  return Math.max(0, Math.min(100, (ms / scale) * 100));
}

/** Whether a turn should be highlighted: the server's flag, or over the slow threshold. */
export function isSlow(turn: LatencyTurn, t: LatencyThresholds = DEFAULT_THRESHOLDS): boolean {
  return turn.slow || (turn.response_ms !== null && turn.response_ms >= t.slow_ms);
}
