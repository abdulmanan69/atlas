/** Form model for the agent editor, and conversion to/from the API's AgentConfig. */
import { z } from "zod";

import type { ResponseSpeed } from "@/features/calls/latency-types";
import { normalizeFlow, type FlowDefinition } from "@/features/voice/flow/types";
import type { AgentConfig, ServiceChoice } from "@/features/voice/queries";

const scalar = z.union([z.string(), z.number(), z.boolean(), z.null()]);
const choice = z.object({ connection_id: z.string().min(1, "Choose a connection"), settings: z.record(z.string(), scalar) });

/** Normalize an API choice (settings optional) into the form shape. */
export function normalizeChoice(c: ServiceChoice | null | undefined): { connection_id: string; settings: Record<string, string | number | boolean | null> } | null {
  return c ? { connection_id: c.connection_id, settings: c.settings ?? {} } : null;
}

const seconds = (min: number, max: number, what: string) =>
  z.number({ error: `Enter ${what} in seconds` }).min(min, `At least ${min} seconds`).max(max, `At most ${max} seconds`);

type TurnFields = {
  turn_start: "default" | "min_words";
  turn_start_min_words: number;
  turn_stop: "transcription" | "turn_analyzer";
  smart_turn_stop_seconds: number;
  idle_timeout_seconds: number;
};

/** What each response-speed preset compiles to (ADR 0006); mirrored by the API. */
export const RESPONSE_SPEEDS: { value: ResponseSpeed; label: string; description: string; fields: TurnFields | null }[] = [
  {
    value: "snappy",
    label: "Snappy",
    description: "Replies as soon as the person pauses. Quick back-and-forth, but may cut in on slow talkers.",
    fields: { turn_start: "min_words", turn_start_min_words: 1, turn_stop: "transcription", smart_turn_stop_seconds: 0.8, idle_timeout_seconds: 8 },
  },
  {
    value: "balanced",
    label: "Balanced",
    description: "Waits for a short pause and ignores a stray “mm-hm”. Right for most calls.",
    fields: { turn_start: "min_words", turn_start_min_words: 2, turn_stop: "transcription", smart_turn_stop_seconds: 1.5, idle_timeout_seconds: 10 },
  },
  {
    value: "patient",
    label: "Patient",
    description: "Waits until the sentence sounds finished. Fewer interruptions, slower replies.",
    fields: { turn_start: "default", turn_start_min_words: 2, turn_stop: "turn_analyzer", smart_turn_stop_seconds: 2.5, idle_timeout_seconds: 15 },
  },
  { value: "custom", label: "Custom", description: "Set turn-taking and the silence limit yourself.", fields: null },
];

export function speedFields(speed: ResponseSpeed): TurnFields | null {
  return RESPONSE_SPEEDS.find((s) => s.value === speed)?.fields ?? null;
}

export const callSettingsSchema = z
  .object({
    response_speed: z.enum(["snappy", "balanced", "patient", "custom"]),
    context_compaction: z.boolean(),
    tts_cache: z.boolean(),
    max_call_seconds: seconds(30, 1200, "the longest call").int("Whole seconds"),
    idle_timeout_seconds: seconds(3, 60, "the silence limit").int("Whole seconds"),
    turn_start: z.enum(["default", "min_words"]),
    turn_start_min_words: z.number({ error: "Enter a number of words" }).int().min(1, "At least 1").max(10, "At most 10"),
    turn_stop: z.enum(["transcription", "turn_analyzer"]),
    smart_turn_stop_seconds: seconds(0.5, 5, "the pause"),
    key_terms: z.array(z.string().trim().min(1).max(60)).max(100),
    voicemail: z.object({
      enabled: z.boolean(),
      action: z.enum(["hangup", "leave_message"]),
      message: z.string().max(1000, "Keep the message under 1000 characters"),
    }),
    ambient: z.object({ enabled: z.boolean(), volume: z.number().min(0).max(1) }),
  })
  .superRefine((v, ctx) => {
    if (v.voicemail.enabled && v.voicemail.action === "leave_message" && !v.voicemail.message.trim())
      ctx.addIssue({ code: "custom", path: ["voicemail", "message"], message: "Write the message to leave" });
  });

export const agentFormSchema = z
  .object({
    mode: z.enum(["guided", "flow"]),
    /** Edited on the canvas; shape-checked there and by the API. */
    flow: z.custom<FlowDefinition>((v) => typeof v === "object" && v !== null).nullable(),
    global_rules: z.string().max(4000, "Keep global rules under 4000 characters"),
    call_settings: callSettingsSchema,
    persona_name: z.string().trim().min(1, "Give the agent a name").max(60),
    company_name: z.string().trim().min(1, "Who is the agent calling for?").max(120),
    language: z.string().regex(/^[a-z]{2,3}(-[A-Za-z]{2,4})?$/, "Use a language code such as en or ur"),
    greeting: z.string().trim().min(1, "Write the first thing the agent says").max(600),
    instructions: z.string().trim().min(20, "Describe the goal in at least 20 characters").max(20_000),
    questions: z.array(z.object({ value: z.string().max(300, "Keep questions under 300 characters") })).max(15),
    closing: z.string().trim().min(1, "Describe how to end the call").max(1000),
    end_condition: z.string().trim().min(1, "Describe when to wrap up").max(500),
    outcomes: z
      .array(
        z.object({
          code: z.string().regex(/^[a-z][a-z0-9_]{0,63}$/, "Lowercase letters, numbers and underscores"),
          description: z.string().trim().min(1, "Describe this outcome").max(300),
        }),
      )
      .max(20),
    capture_fields: z
      .array(
        z.object({
          name: z.string().regex(/^[a-z][a-z0-9_]{0,47}$/, "Lowercase letters, numbers and underscores"),
          type: z.enum(["string", "number", "boolean"]),
          description: z.string().trim().min(1, "Say what to capture").max(300),
        }),
      )
      .max(20),
    allow_interrupt: z.boolean(),
    ai_disclosure: z.boolean(),
    override_llm: choice.nullable(),
    override_tts: choice.nullable(),
  })
  .superRefine((v, ctx) => {
    const codes = v.outcomes.map((o) => o.code);
    codes.forEach((c, i) => {
      if (codes.indexOf(c) !== i) ctx.addIssue({ code: "custom", path: ["outcomes", i, "code"], message: "Duplicate code" });
    });
    const names = v.capture_fields.map((f) => f.name);
    names.forEach((n, i) => {
      if (names.indexOf(n) !== i || n === "opted_out")
        ctx.addIssue({ code: "custom", path: ["capture_fields", i, "name"], message: n === "opted_out" ? "Reserved name" : "Duplicate name" });
    });
    if (v.mode === "flow" && !v.flow) ctx.addIssue({ code: "custom", path: ["flow"], message: "The flow is missing" });
  });

export type AgentForm = z.infer<typeof agentFormSchema>;
export type CallSettingsForm = z.infer<typeof callSettingsSchema>;

/**
 * Mirrors the API: settings saved before presets existed (turn fields present, no
 * response_speed) are read as Custom, so their explicit turn-taking keeps working.
 */
export function storedSpeed(cs: AgentConfig["call_settings"]): ResponseSpeed {
  if (cs?.response_speed) return cs.response_speed;
  const legacy = cs && ["turn_start", "turn_start_min_words", "turn_stop", "smart_turn_stop_seconds"].some((k) => (cs as Record<string, unknown>)[k] !== undefined);
  return legacy ? "custom" : "balanced";
}

export function toForm(c: AgentConfig): AgentForm {
  const cs = c.call_settings;
  return {
    mode: c.mode ?? "guided",
    flow: c.mode === "flow" ? normalizeFlow(c.flow) : null,
    global_rules: c.global_rules ?? "",
    call_settings: {
      response_speed: storedSpeed(cs),
      context_compaction: cs?.context_compaction ?? true,
      tts_cache: cs?.tts_cache ?? false,
      max_call_seconds: cs?.max_call_seconds ?? 300,
      idle_timeout_seconds: cs?.idle_timeout_seconds ?? 10,
      turn_start: cs?.turn_start ?? "default",
      turn_start_min_words: cs?.turn_start_min_words ?? 2,
      turn_stop: cs?.turn_stop ?? "transcription",
      smart_turn_stop_seconds: cs?.smart_turn_stop_seconds ?? 2,
      key_terms: [...(cs?.key_terms ?? [])],
      voicemail: { enabled: cs?.voicemail?.enabled ?? true, action: cs?.voicemail?.action ?? "hangup", message: cs?.voicemail?.message ?? "" },
      ambient: { enabled: cs?.ambient?.enabled ?? false, volume: cs?.ambient?.volume ?? 0.3 },
    },
    persona_name: c.persona_name ?? "Alex",
    company_name: c.company_name,
    language: c.language ?? "en",
    greeting: c.greeting,
    instructions: c.instructions,
    questions: (c.qualification_questions ?? []).map((value) => ({ value })),
    closing: c.closing ?? "",
    end_condition: c.end_condition ?? "",
    outcomes: (c.outcomes ?? []).map((o) => ({ code: o.code, description: o.description })),
    capture_fields: (c.capture_fields ?? []).map((f) => ({ name: f.name, type: f.type ?? "string", description: f.description })),
    allow_interrupt: c.allow_interrupt ?? true,
    ai_disclosure: c.ai_disclosure ?? true,
    override_llm: normalizeChoice(c.model_override?.llm),
    override_tts: normalizeChoice(c.model_override?.tts),
  };
}

export function fromForm(f: AgentForm): AgentConfig {
  const override = f.override_llm || f.override_tts ? { llm: f.override_llm, tts: f.override_tts } : null;
  const flow = f.mode === "flow" && f.flow ? { nodes: f.flow.nodes, paths: f.flow.paths, viewport: null } : null;
  return {
    mode: f.mode,
    flow,
    global_rules: f.global_rules.trim() || null,
    call_settings: {
      ...f.call_settings,
      voicemail: { ...f.call_settings.voicemail, message: f.call_settings.voicemail.message.trim() || null },
    },
    persona_name: f.persona_name,
    company_name: f.company_name,
    language: f.language,
    greeting: f.greeting,
    instructions: f.instructions,
    qualification_questions: f.questions.map((q) => q.value.trim()).filter(Boolean),
    closing: f.closing,
    end_condition: f.end_condition,
    outcomes: f.outcomes,
    capture_fields: f.capture_fields,
    allow_interrupt: f.allow_interrupt,
    ai_disclosure: f.ai_disclosure,
    model_override: override,
  };
}

export const LANGUAGES: [string, string][] = [
  ["en", "English"],
  ["ur", "Urdu"],
  ["hi", "Hindi"],
  ["ar", "Arabic"],
  ["es", "Spanish"],
  ["fr", "French"],
  ["de", "German"],
  ["pt", "Portuguese"],
  ["tr", "Turkish"],
];

export const VARIABLES = ["first_name", "last_name", "full_name", "company", "job_title", "email"];
