/**
 * Pure configuration for the calls feature: URL state ↔ API filters, the mapping between
 * AG Grid's Infinite Row Model and `GET /api/v1/calls`, badge semantics for state, mode
 * and outcome, and the transcript parser. Component-free so it is unit-testable.
 */
import type { IDatasource, IGetRowsParams } from "ag-grid-community";

import type { Call, CallQuery } from "@/features/queries";

export const BLOCK_SIZE = 50;

export type CallState = Call["state"];
export type CallMode = Call["mode"];
export type CallDirection = Call["direction"];
export type CallFilters = Omit<CallQuery, "page" | "page_size">;
export interface CallPage {
  items: Call[];
  total: number;
}

type Tone = "neutral" | "accent" | "success" | "warn" | "danger";

export const CALL_STATES: CallState[] = ["queued", "dialing", "in_progress", "completed", "no_answer", "busy", "failed", "canceled"];
export const TERMINAL_STATES: ReadonlySet<CallState> = new Set<CallState>(["completed", "no_answer", "busy", "failed", "canceled"]);

export const STATE_BADGE: Record<CallState, { label: string; tone: Tone }> = {
  queued: { label: "Queued", tone: "neutral" },
  dialing: { label: "Dialing", tone: "accent" },
  in_progress: { label: "In progress", tone: "accent" },
  completed: { label: "Answered", tone: "success" },
  no_answer: { label: "No answer", tone: "neutral" },
  busy: { label: "Busy", tone: "warn" },
  failed: { label: "Failed", tone: "danger" },
  canceled: { label: "Canceled", tone: "neutral" },
};

export const MODE_BADGE: Record<CallMode, { label: string; tone: Tone; description: string }> = {
  live: { label: "Live", tone: "success", description: "A real phone call was placed." },
  simulated: {
    label: "Simulated",
    tone: "neutral",
    description: "A dry run: no phone call was placed. The outcome and transcript are synthetic and clearly labelled.",
  },
  test: {
    label: "Test",
    tone: "accent",
    description: "A browser test call from the agent editor: no phone call was placed and no lead was contacted. Never counted in reports.",
  },
};

export const DIRECTION_LABEL: Record<CallDirection, string> = { outbound: "Outbound", inbound: "Inbound" };

/** "Browser" for a test call (the tester talked through the microphone), otherwise the direction. */
export function directionLabel(call: Pick<Call, "mode" | "direction">): string {
  return call.mode === "test" ? "Browser" : DIRECTION_LABEL[call.direction];
}

const OUTCOME_TONES: Record<string, Tone> = {
  qualified: "success",
  interested: "success",
  appointment_booked: "success",
  callback: "accent",
  callback_requested: "accent",
  not_interested: "neutral",
  unqualified: "neutral",
  wrong_number: "warn",
  do_not_call: "danger",
  opted_out: "danger",
};

export function outcomeTone(outcome: string | null | undefined): Tone {
  return outcome ? (OUTCOME_TONES[outcome] ?? "neutral") : "neutral";
}

/** `appointment_booked` → `Appointment booked`. Codes are the agent's own, so keep them recognisable. */
export function outcomeLabel(outcome: string | null | undefined): string {
  if (!outcome) return "—";
  const words = outcome.replace(/[_-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/* URL state */

export type ModeChoice = "all" | CallMode;

export interface CallsUrlState {
  from: string | null;
  to: string | null;
  agent: string | null;
  campaign: string | null;
  lead: string | null;
  state: CallState | null;
  outcome: string | null;
  mode: ModeChoice;
  direction: CallDirection | null;
}

const isState = (v: string | null): v is CallState => v !== null && (CALL_STATES as string[]).includes(v);
const isDirection = (v: string | null): v is CallDirection => v === "outbound" || v === "inbound";
const isMode = (v: string | null): v is CallMode => v === "live" || v === "simulated" || v === "test";
const isIso = (v: string | null): v is string => v !== null && !Number.isNaN(new Date(v).getTime());

export function readCallsUrl(params: URLSearchParams): CallsUrlState {
  const mode = params.get("mode");
  const from = params.get("from");
  const to = params.get("to");
  const state = params.get("state");
  const direction = params.get("direction");
  return {
    from: isIso(from) ? from : null,
    to: isIso(to) ? to : null,
    agent: params.get("agent") || null,
    campaign: params.get("campaign") || null,
    lead: params.get("lead") || null,
    state: isState(state) ? state : null,
    outcome: params.get("outcome") || null,
    mode: isMode(mode) ? mode : "all",
    direction: isDirection(direction) ? direction : null,
  };
}

/** URL state → the API's query parameters. Only set keys are sent. */
export function urlToFilters(u: CallsUrlState): CallFilters {
  const f: CallFilters = {};
  if (u.from) f.created_from = u.from;
  if (u.to) f.created_to = u.to;
  if (u.agent) f.agent_id = u.agent;
  if (u.campaign) f.campaign_id = u.campaign;
  if (u.lead) f.lead_id = u.lead;
  if (u.state) f.state = u.state;
  if (u.outcome) f.outcome = u.outcome;
  if (u.mode !== "all") f.mode = u.mode;
  if (u.direction) f.direction = u.direction;
  return f;
}

export function filtersActive(u: CallsUrlState): boolean {
  return Boolean(u.from || u.to || u.agent || u.campaign || u.lead || u.state || u.outcome || u.direction || u.mode !== "all");
}

export type DatePreset = "today" | "yesterday" | "7d" | "30d" | "month";
export const DATE_PRESETS: { id: DatePreset; label: string }[] = [
  { id: "today", label: "Today" },
  { id: "yesterday", label: "Yesterday" },
  { id: "7d", label: "Last 7 days" },
  { id: "30d", label: "Last 30 days" },
  { id: "month", label: "This month" },
];

/** Local-time boundaries for a preset as ISO strings; `to` is null when the range is open-ended. */
export function presetRange(preset: DatePreset, now: Date = new Date()): { from: string; to: string | null } {
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const today = startOfDay(now);
  const days = (n: number) => new Date(today.getFullYear(), today.getMonth(), today.getDate() + n);
  switch (preset) {
    case "today":
      return { from: today.toISOString(), to: null };
    case "yesterday":
      return { from: days(-1).toISOString(), to: today.toISOString() };
    case "7d":
      return { from: days(-6).toISOString(), to: null };
    case "30d":
      return { from: days(-29).toISOString(), to: null };
    case "month":
      return { from: new Date(today.getFullYear(), today.getMonth(), 1).toISOString(), to: null };
  }
}

/* Grid mapping */

/** One infinite-model block → one API page. The list is always newest first. */
export function blockToQuery(block: Pick<IGetRowsParams, "startRow" | "endRow">, filters: CallFilters): CallQuery {
  const page_size = Math.max(1, block.endRow - block.startRow);
  return { ...filters, page: Math.floor(block.startRow / page_size) + 1, page_size };
}

export function createCallsDatasource(opts: {
  fetchPage: (query: CallQuery) => Promise<CallPage>;
  getFilters: () => CallFilters;
  onStart?: () => void;
  onResult?: (page: CallPage) => void;
  onError?: (error: unknown) => void;
}): IDatasource {
  return {
    getRows(params) {
      opts.onStart?.();
      opts.fetchPage(blockToQuery(params, opts.getFilters())).then(
        (page) => {
          params.successCallback(page.items, page.total);
          opts.onResult?.(page);
        },
        (error: unknown) => {
          params.failCallback();
          opts.onError?.(error);
        },
      );
    },
  };
}

/** Columns a user may hide. "When" and "Lead" are always visible. */
export const OPTIONAL_COLUMNS: { colId: string; label: string }[] = [
  { colId: "agent", label: "Agent" },
  { colId: "direction", label: "Direction" },
  { colId: "mode", label: "Mode" },
  { colId: "state", label: "State" },
  { colId: "outcome", label: "Outcome" },
  { colId: "duration", label: "Duration" },
  { colId: "captured", label: "Captured" },
];

/* Transcript */

export type TranscriptTurn = { role: "agent" | "person" | "note"; speaker: string | null; text: string };
export type ParsedTranscript = { kind: "turns"; turns: TranscriptTurn[] } | { kind: "plain"; text: string };

const SPEAKER_LINE = /^\s*([A-Za-z][\w .'-]{0,39}):\s?(.*)$/;

/**
 * Lines prefixed `Agent:` are the agent's turns and any other `Name:` prefix is the
 * person's. Unprefixed lines continue the previous turn, or stand alone as notes (for
 * example the `[SIMULATED CALL]` marker). Text without any `Agent:` line is left as is.
 */
export function parseTranscript(text: string): ParsedTranscript {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  if (!lines.some((l) => /^\s*agent:/i.test(l))) return { kind: "plain", text };
  const turns: TranscriptTurn[] = [];
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const m = SPEAKER_LINE.exec(line);
    if (m) {
      const speaker = m[1]!.trim();
      turns.push({ role: /^agent$/i.test(speaker) ? "agent" : "person", speaker, text: m[2]!.trim() });
      continue;
    }
    const last = turns[turns.length - 1];
    if (last && last.role !== "note") last.text = last.text ? `${last.text}\n${line.trim()}` : line.trim();
    else turns.push({ role: "note", speaker: null, text: line.trim() });
  }
  return { kind: "turns", turns };
}

/** `state_changed` → `State changed`. */
export function eventLabel(type: string): string {
  const words = type.replace(/[_.-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Flattens a usage/cost object into `key.sub → value` rows for a definition list (two levels). */
export function flattenUsage(value: Record<string, unknown>, prefix = ""): { key: string; value: string }[] {
  const rows: { key: string; value: string }[] = [];
  for (const [k, v] of Object.entries(value)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      if (prefix) rows.push({ key, value: JSON.stringify(v) });
      else rows.push(...flattenUsage(v as Record<string, unknown>, key));
    } else if (v === null || v === undefined) {
      rows.push({ key, value: "—" });
    } else {
      rows.push({ key, value: typeof v === "number" ? String(Math.round(v * 1000) / 1000) : Array.isArray(v) ? v.join(", ") : String(v) });
    }
  }
  return rows;
}
