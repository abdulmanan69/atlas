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

export type TranscriptTurn = {
  role: "agent" | "person" | "note";
  speaker: string | null;
  text: string;
  /** Seconds from the start of the conversation, when the transcript carries times. */
  offset?: number | null;
};
export type ParsedTranscript = { kind: "turns"; turns: TranscriptTurn[] } | { kind: "plain"; text: string };

const SPEAKER_LINE = /^\s*([A-Za-z][\w .'-]{0,39}):\s?(.*)$/;
/** `[2026-10-19T16:50:50.565+00:00 → 2026-10-19T16:50:52.894+00:00] user: Yes.` (end optional). */
const TIMED_LINE = /^\s*\[([^\]\s→]+)(?:\s*(?:→|->)\s*([^\]\s]+))?\]\s*([A-Za-z][\w .'-]{0,39}):\s?(.*)$/;
const AGENT_ROLES = /^(agent|assistant|bot)$/i;

/** One utterance, with each repeated line kept once (the voice service can log a phrase twice). */
function dedupeLines(text: string): string {
  const out: string[] = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (t && out[out.length - 1]?.toLowerCase() !== t.toLowerCase()) out.push(t);
  }
  return out.join("\n");
}

/**
 * Two formats are recognised:
 * - Voice-service transcripts: `[start → end] assistant|user: text`, with continuation lines.
 *   Lines are put back in time order (the service can write a reply before the words it
 *   answered) and each turn carries its offset from the first line.
 * - Simulated transcripts: `Agent:` for the agent, any other `Name:` for the person;
 *   unprefixed lines continue the previous turn or stand alone as notes.
 * Anything else is shown as plain text.
 */
export function parseTranscript(text: string): ParsedTranscript {
  const lines = text.replace(/\r\n?/g, "\n").split("\n");
  if (lines.some((l) => TIMED_LINE.test(l))) return parseTimed(lines);
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

function parseTimed(lines: string[]): ParsedTranscript {
  const items: { at: number; seq: number; role: "agent" | "person"; text: string }[] = [];
  for (const raw of lines) {
    if (!raw.trim()) continue;
    const m = TIMED_LINE.exec(raw);
    if (m) {
      const at = Date.parse(m[1]!);
      items.push({
        at: Number.isNaN(at) ? Number.POSITIVE_INFINITY : at,
        seq: items.length,
        role: AGENT_ROLES.test(m[3]!.trim()) ? "agent" : "person",
        text: m[4]!.trim(),
      });
    } else if (items.length) {
      const last = items[items.length - 1]!;
      last.text = last.text ? `${last.text}\n${raw.trim()}` : raw.trim();
    }
  }
  items.sort((a, b) => a.at - b.at || a.seq - b.seq);
  const first = items.find((i) => Number.isFinite(i.at));
  const start = first ? first.at : null;
  const turns: TranscriptTurn[] = items
    .map((i) => ({
      role: i.role,
      speaker: i.role === "agent" ? "Agent" : "Caller",
      text: dedupeLines(i.text),
      offset: start !== null && Number.isFinite(i.at) ? Math.max(0, (i.at - start) / 1000) : null,
    }))
    .filter((t) => t.text);
  return turns.length ? { kind: "turns", turns } : { kind: "plain", text: lines.join("\n") };
}

/** 75 → `1:15`. */
export function formatOffset(seconds: number): string {
  const s = Math.floor(seconds);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** `state_changed` → `State changed`. */
export function eventLabel(type: string): string {
  const words = type.replace(/[_.-]+/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Flattens a usage/cost object into `key.sub → value` rows for a definition list (two levels). */
export type UsageRow = { label: string; model: string | null; value: string };

const USAGE_KIND: Record<string, string> = { llm: "Language model", stt: "Speech-to-text", tts: "Voice" };
/** Processor classes of self-hosted model servers; they have no per-use charge. */
const SELF_HOSTED_PROCESSOR = /^Speaches/i;

function round(n: number, digits = 1): string {
  return n.toLocaleString(undefined, { maximumFractionDigits: digits });
}

/** `SpeachesLLMService#3|||qwen2.5:1.5b` → model `qwen2.5:1.5b`, and whether it is self-hosted. */
function splitUsageKey(key: string): { model: string | null; selfHosted: boolean } {
  const [processor = "", model = ""] = key.split("|||");
  const fallback = processor.includes("Service") ? null : processor.trim() || null;
  return { model: model.trim() || fallback, selfHosted: SELF_HOSTED_PROCESSOR.test(processor) };
}

function describe(kind: string, v: unknown): string {
  if (typeof v === "number") {
    if (kind === "stt") return `${round(v)} s of audio transcribed`;
    if (kind === "tts") return `${round(v, 0)} characters spoken`;
    return round(v, 2);
  }
  if (v && typeof v === "object") {
    const t = v as Record<string, unknown>;
    const n = (k: string) => (typeof t[k] === "number" ? (t[k] as number) : null);
    const total = n("total_tokens");
    const prompt = n("prompt_tokens");
    const completion = n("completion_tokens");
    const cached = n("cache_read_input_tokens");
    if (total !== null || prompt !== null || completion !== null) {
      const parts = [
        prompt !== null ? `${round(prompt, 0)} in` : null,
        completion !== null ? `${round(completion, 0)} out` : null,
        cached ? `${round(cached, 0)} reused from cache` : null,
      ].filter(Boolean);
      const sum = total ?? (prompt ?? 0) + (completion ?? 0);
      return `${round(sum, 0)} tokens${parts.length ? ` (${parts.join(", ")})` : ""}`;
    }
    const known = Object.entries(t).filter(([, x]) => typeof x === "number" || typeof x === "string");
    return known.length
      ? known.map(([k, x]) => `${k.replace(/_/g, " ")}: ${typeof x === "number" ? round(x, 2) : String(x)}`).join(", ")
      : "—";
  }
  return v === null || v === undefined ? "—" : String(v);
}

/**
 * The voice service reports usage as `{llm|stt|tts: {"<processor>|||<model>": value}}` plus
 * `call_duration_seconds`. Shown as one plain row per model; internal processor names and
 * the duration (shown elsewhere) are left out. Unknown top-level keys still get a row.
 */
export function summarizeUsage(usage: Record<string, unknown> | null | undefined): { rows: UsageRow[]; selfHostedOnly: boolean } {
  const rows: UsageRow[] = [];
  let any = false;
  let allSelfHosted = true;
  for (const [kind, value] of Object.entries(usage ?? {})) {
    if (kind === "call_duration_seconds") continue;
    const label = USAGE_KIND[kind] ?? kind.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
    if (value && typeof value === "object" && !Array.isArray(value) && kind in USAGE_KIND) {
      for (const [key, v] of Object.entries(value as Record<string, unknown>)) {
        const { model, selfHosted } = splitUsageKey(key);
        any = true;
        allSelfHosted = allSelfHosted && selfHosted;
        rows.push({ label, model, value: describe(kind, v) });
      }
    } else {
      rows.push({ label, model: null, value: describe(kind, value) });
    }
  }
  return { rows, selfHostedOnly: any && allSelfHosted };
}
