/**
 * Pure campaign configuration: status/mode/lead-status semantics, the allowed
 * lifecycle moves (mirrors `TRANSITIONS` in the API), the calling-hours editor model,
 * the wizard's zod schema and the request-body builders. Component-free.
 */
import { z } from "zod";

import type { Campaign, CampaignLead, CampaignProgress } from "@/features/queries";
import type { Schemas } from "@/lib/api";

type Tone = "neutral" | "accent" | "success" | "warn" | "danger";

export type CampaignStatus = Campaign["status"];
export type CampaignMode = Campaign["mode"];
export type CampaignLeadStatus = CampaignLead["status"];
export type CallingHours = Schemas["CallingHours"];
export type LeadFilterSpec = Schemas["LeadFilterSpec"];
export type RetryTrigger = "no_answer" | "busy" | "failed";

export const STATUS_BADGE: Record<CampaignStatus, { label: string; tone: Tone }> = {
  draft: { label: "Draft", tone: "neutral" },
  scheduled: { label: "Scheduled", tone: "accent" },
  running: { label: "Running", tone: "success" },
  paused: { label: "Paused", tone: "warn" },
  completed: { label: "Completed", tone: "neutral" },
  canceled: { label: "Canceled", tone: "neutral" },
  failed: { label: "Failed", tone: "danger" },
};

export const MODE_BADGE: Record<CampaignMode, { label: string; tone: Tone; description: string }> = {
  dry_run: { label: "Dry run", tone: "neutral", description: "Simulated calls only. Nothing leaves the platform; outcomes are synthetic." },
  live: { label: "Live", tone: "success", description: "Real phone calls are placed to the audience." },
};

/** Segment colour classes follow the StageTrack language: success / accent / neutral / lost / danger / warn. */
export const LEAD_STATUS: Record<CampaignLeadStatus, { label: string; tone: Tone; bar: string; description: string }> = {
  completed: { label: "Completed", tone: "success", bar: "bg-success", description: "Answered and finished" },
  calling: { label: "Calling", tone: "accent", bar: "bg-accent", description: "A call is queued, dialing or in progress" },
  pending: { label: "Pending", tone: "neutral", bar: "bg-line-strong", description: "Waiting for a dispatch slot or a retry time" },
  skipped: { label: "Skipped", tone: "neutral", bar: "bg-lost", description: "Never called; see the reason" },
  exhausted: { label: "Exhausted", tone: "warn", bar: "bg-warn", description: "Retriable results, but the retry budget is used up" },
  failed: { label: "Failed", tone: "danger", bar: "bg-danger", description: "The last attempt failed for good" },
};
export const LEAD_STATUS_ORDER: CampaignLeadStatus[] = ["completed", "calling", "pending", "skipped", "exhausted", "failed"];

export type LifecycleAction = "start" | "pause" | "resume" | "cancel";

const ALLOWED: Record<LifecycleAction, CampaignStatus[]> = {
  start: ["draft"],
  pause: ["scheduled", "running"],
  resume: ["paused"],
  cancel: ["draft", "scheduled", "running", "paused"],
};

export function canTransition(status: CampaignStatus, action: LifecycleAction): boolean {
  return ALLOWED[action].includes(status);
}

export const isTerminal = (status: CampaignStatus): boolean => status === "completed" || status === "canceled" || status === "failed";
export const isActive = (status: CampaignStatus): boolean => status === "scheduled" || status === "running";
/** Agent, mode and audience are frozen once the audience has been snapshotted. */
export const isLocked = (status: CampaignStatus): boolean => status !== "draft";

const SKIP_REASONS: Record<string, string> = {
  do_not_call: "Do not call",
  consent_revoked: "Consent revoked",
  no_phone: "No phone number",
  campaign_canceled: "Campaign canceled",
  usage_limit_reached: "Call limit reached",
  campaign_window_ended: "Window ended",
};
export function skipReasonLabel(reason: string | null | undefined): string {
  if (!reason) return "—";
  return SKIP_REASONS[reason] ?? reason.replace(/_/g, " ");
}

/* Calling hours */

export const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"] as const;
export type Weekday = (typeof WEEKDAYS)[number];
export const DAY_LABELS: Record<Weekday, string> = { mon: "Monday", tue: "Tuesday", wed: "Wednesday", thu: "Thursday", fri: "Friday", sat: "Saturday", sun: "Sunday" };

export interface DayWindow {
  enabled: boolean;
  from: string;
  to: string;
}
export type HoursDraft = Record<Weekday, DayWindow>;

/** Half-hour steps; `24:00` is allowed as the end of a window only. */
export const TIME_OPTIONS: string[] = Array.from({ length: 48 }, (_, i) => `${String(Math.floor(i / 2)).padStart(2, "0")}:${i % 2 ? "30" : "00"}`);
export const END_TIME_OPTIONS: string[] = [...TIME_OPTIONS.slice(1), "24:00"];

export const DEFAULT_HOURS: HoursDraft = Object.fromEntries(
  WEEKDAYS.map((d) => [d, { enabled: d !== "sat" && d !== "sun", from: "09:00", to: "18:00" }]),
) as HoursDraft;

/** API hours → editor rows. Only the first window per day is editable here; others are kept untouched on save. */
export function hoursToDraft(hours: CallingHours | null | undefined): HoursDraft {
  const draft = { ...DEFAULT_HOURS };
  if (!hours) return draft; // a new campaign starts from the weekday default
  for (const d of WEEKDAYS) {
    const first = hours?.[d]?.[0];
    draft[d] = first ? { enabled: true, from: first[0], to: first[1] } : { ...DEFAULT_HOURS[d], enabled: false };
  }
  return draft;
}

export function draftToHours(draft: HoursDraft, previous?: CallingHours | null): CallingHours {
  const out: CallingHours = {};
  for (const d of WEEKDAYS) {
    const w = draft[d];
    if (!w.enabled) {
      out[d] = [];
      continue;
    }
    const rest = (previous?.[d] ?? []).slice(1);
    out[d] = [[w.from, w.to], ...rest];
  }
  return out;
}

export function hoursError(draft: HoursDraft): string | null {
  const enabled = WEEKDAYS.filter((d) => draft[d].enabled);
  if (enabled.length === 0) return "Pick at least one day on which calls may be placed.";
  for (const d of enabled) {
    if (draft[d].from >= draft[d].to) return `${DAY_LABELS[d]}: the window must start before it ends.`;
  }
  return null;
}

/** `{"mon":[["09:00","18:00"]], ...}` → "Mon–Fri 09:00–18:00" style summary. */
export function summarizeHours(hours: CallingHours): string {
  const short: Record<Weekday, string> = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
  const groups: { days: Weekday[]; window: string }[] = [];
  for (const d of WEEKDAYS) {
    const windows = hours[d] ?? [];
    if (windows.length === 0) continue;
    const window = windows.map(([a, b]) => `${a}–${b}`).join(", ");
    const last = groups[groups.length - 1];
    const prev = last?.days[last.days.length - 1];
    if (last && last.window === window && prev && WEEKDAYS.indexOf(prev) === WEEKDAYS.indexOf(d) - 1) last.days.push(d);
    else groups.push({ days: [d], window });
  }
  if (groups.length === 0) return "No calling hours";
  return groups.map((g) => `${g.days.length > 2 ? `${short[g.days[0]!]}–${short[g.days[g.days.length - 1]!]}` : g.days.map((x) => short[x]).join(", ")} ${g.window}`).join(" · ");
}

/* Wizard form */

const dayWindow = z.object({ enabled: z.boolean(), from: z.string(), to: z.string() });
const hoursDraft = z.object({ mon: dayWindow, tue: dayWindow, wed: dayWindow, thu: dayWindow, fri: dayWindow, sat: dayWindow, sun: dayWindow });

const int = (min: number, max: number, label: string) =>
  z
    .union([z.string(), z.number()])
    .transform((v) => (typeof v === "string" ? v.trim() : String(v)))
    .pipe(z.string().regex(/^\d+$/, `${label} must be a whole number`))
    .transform(Number)
    .pipe(z.number().int().min(min, `${label} must be at least ${min}`).max(max, `${label} must be at most ${max}`));

export const wizardSchema = z
  .object({
    // 1 · Basics
    name: z.string().trim().min(1, "Name the campaign").max(120, "Keep the name under 120 characters"),
    objective: z.string().trim().max(2000, "Keep the objective under 2000 characters"),
    agent_id: z.string().min(1, "Choose an agent"),
    // 2 · Audience
    stage_id: z.string(),
    owner: z.string(),
    tag: z
      .string()
      .trim()
      .toLowerCase()
      .max(64)
      .refine((v) => v === "" || /^[a-z0-9][a-z0-9_\-:]{0,63}$/.test(v), "Tags use letters, numbers, - _ :"),
    source: z.string().trim().max(80),
    qualification_status: z.string(),
    // 3 · Schedule
    start_at: z.string(),
    end_at: z.string(),
    timezone: z.string().min(1, "Choose a time zone"),
    hours: hoursDraft,
    // 4 · Pacing & retries
    max_concurrency: int(1, 50, "Concurrency"),
    calls_per_minute: int(1, 60, "Calls per minute"),
    max_calls: z
      .union([z.string(), z.number()])
      .transform((v) => (typeof v === "string" ? v.trim() : String(v)))
      .pipe(z.string().regex(/^\d*$/, "Max calls must be a whole number"))
      .transform((v) => (v === "" ? null : Number(v)))
      .pipe(z.number().int().min(1, "Max calls must be at least 1").nullable()),
    max_attempts: int(1, 5, "Attempts"),
    retry_on: z.array(z.enum(["no_answer", "busy", "failed"])),
    backoff_minutes: int(1, 1440, "Backoff"),
  })
  .superRefine((v, ctx) => {
    if (!v.stage_id && !v.owner && !v.tag && !v.source && !v.qualification_status) {
      ctx.addIssue({ code: "custom", path: ["stage_id"], message: "Choose at least one filter so the audience is explicit." });
    }
    if (v.start_at && v.end_at && new Date(v.end_at) <= new Date(v.start_at)) {
      ctx.addIssue({ code: "custom", path: ["end_at"], message: "The end must be after the start." });
    }
    const hours = hoursError(v.hours);
    if (hours) ctx.addIssue({ code: "custom", path: ["hours"], message: hours });
  });

export type WizardInput = z.input<typeof wizardSchema>;
export type WizardOutput = z.output<typeof wizardSchema>;

export const STEP_FIELDS: readonly (keyof WizardInput)[][] = [
  ["name", "objective", "agent_id"],
  ["stage_id", "owner", "tag", "source", "qualification_status"],
  ["start_at", "end_at", "timezone", "hours"],
  ["max_concurrency", "calls_per_minute", "max_calls", "max_attempts", "retry_on", "backoff_minutes"],
  [],
];

export function wizardDefaults(timezone: string, campaign?: Campaign): WizardInput {
  const f = campaign?.lead_filter;
  const retry = campaign?.retry_policy;
  return {
    name: campaign?.name ?? "",
    objective: campaign?.objective ?? "",
    agent_id: campaign?.agent_id ?? "",
    stage_id: f?.stage_id ?? "",
    owner: f?.unassigned ? "unassigned" : (f?.owner_user_id ?? ""),
    tag: f?.tag ?? "",
    source: f?.source ?? "",
    qualification_status: f?.qualification_status ?? "",
    start_at: campaign?.start_at ?? "",
    end_at: campaign?.end_at ?? "",
    timezone: campaign?.timezone ?? timezone,
    hours: hoursToDraft(campaign?.calling_hours),
    max_concurrency: String(campaign?.max_concurrency ?? 2),
    calls_per_minute: String(campaign?.calls_per_minute ?? 10),
    max_calls: campaign?.max_calls === null || campaign?.max_calls === undefined ? "" : String(campaign.max_calls),
    max_attempts: String(retry?.max_attempts ?? 2),
    retry_on: retry?.retry_on ?? ["no_answer", "busy"],
    backoff_minutes: String(retry?.backoff_minutes ?? 30),
  };
}

export function audienceFilter(v: Pick<WizardOutput, "stage_id" | "owner" | "tag" | "source" | "qualification_status">): LeadFilterSpec {
  return {
    stage_id: v.stage_id || null,
    owner_user_id: v.owner && v.owner !== "unassigned" ? v.owner : null,
    unassigned: v.owner === "unassigned",
    tag: v.tag || null,
    source: v.source || null,
    qualification_status: (v.qualification_status || null) as LeadFilterSpec["qualification_status"],
  };
}

export function createBody(v: WizardOutput, previous?: CallingHours | null): Schemas["CampaignCreate"] {
  return {
    name: v.name,
    objective: v.objective || null,
    agent_id: v.agent_id,
    mode: "dry_run",
    lead_filter: audienceFilter(v),
    start_at: v.start_at || null,
    end_at: v.end_at || null,
    timezone: v.timezone || null,
    calling_hours: draftToHours(v.hours, previous),
    max_concurrency: v.max_concurrency,
    calls_per_minute: v.calls_per_minute,
    retry_policy: { max_attempts: v.max_attempts, retry_on: v.retry_on, backoff_minutes: v.backoff_minutes },
    max_calls: v.max_calls,
  };
}

/** The PATCH body for one wizard step. Locked fields are left out once the campaign has started. */
export function stepPatch(step: number, v: WizardOutput, locked: boolean, previous?: CallingHours | null): Schemas["CampaignUpdate"] {
  switch (step) {
    case 0:
      return locked ? { name: v.name, objective: v.objective || null } : { name: v.name, objective: v.objective || null, agent_id: v.agent_id };
    case 1:
      return locked ? {} : { lead_filter: audienceFilter(v) };
    case 2:
      return { start_at: v.start_at || null, end_at: v.end_at || null, timezone: v.timezone || null, calling_hours: draftToHours(v.hours, previous) };
    case 3:
      return {
        max_concurrency: v.max_concurrency,
        calls_per_minute: v.calls_per_minute,
        max_calls: v.max_calls,
        retry_policy: { max_attempts: v.max_attempts, retry_on: v.retry_on, backoff_minutes: v.backoff_minutes },
      };
    default:
      return {};
  }
}

/* Progress */

export type LeadCounts = Record<CampaignLeadStatus, number>;

export function leadCounts(progress: CampaignProgress | undefined, campaign?: Campaign): LeadCounts {
  const src = (progress?.leads ?? (campaign?.stats as { leads?: Partial<LeadCounts> } | undefined)?.leads ?? {}) as Partial<LeadCounts>;
  return Object.fromEntries(LEAD_STATUS_ORDER.map((s) => [s, src[s] ?? 0])) as LeadCounts;
}

export const totalLeads = (c: LeadCounts): number => LEAD_STATUS_ORDER.reduce((a, s) => a + c[s], 0);
/** Leads the campaign has dealt with: everything that is no longer waiting. */
export const settledLeads = (c: LeadCounts): number => totalLeads(c) - c.pending - c.calling;

/** Rough ETA as text: "about 12 min", "about 2 h", or null when unknown. */
export function formatEta(seconds: number | null | undefined): string | null {
  if (seconds === null || seconds === undefined) return null;
  if (seconds < 60) return "under a minute";
  if (seconds < 3600) return `about ${Math.round(seconds / 60)} min`;
  const h = seconds / 3600;
  return `about ${h < 10 ? h.toFixed(1).replace(/\.0$/, "") : Math.round(h)} h`;
}
