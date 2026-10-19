/**
 * The test panel's live transcript as a pure reducer: events in, bubbles out.
 *
 * Two sources feed it:
 * - Voice (Speak): the voice service's live feedback, pushed on the call's signalling
 *   socket as `{type: "rtf-…", payload, node_name?}` (lib/webrtc.ts). Verified order of
 *   one answered turn on a real call (docs/decisions/0006-low-latency-voice.md):
 *   `rtf-ttfb-metric` (stt) → `rtf-user-transcription` (final; interim ones before it
 *   with `final: false` on streaming speech recognition) → `rtf-ttfb-metric` (llm) →
 *   `rtf-ttfb-metric` (tts) → `rtf-bot-started-speaking` → `rtf-latency-measured` →
 *   `rtf-bot-text` (one per sentence) → `rtf-bot-stopped-speaking`. Live messages carry no
 *   turn number, so measurements are held until the reply they belong to appears.
 * - Text (Type): ATLAS's own test-chat events (`TestChatEvent`) plus what the tester typed.
 *
 * Wording never repeats the voice service's own error text or names.
 */
import { DEFAULT_THRESHOLDS, responseTone, type SummaryTone } from "@/features/calls/latency-model";
import type { Schemas } from "@/lib/api";
import type { FeedbackMessage } from "@/lib/webrtc";

export type TestChannel = "voice" | "text";
export type ChatEvent = Schemas["TestChatEvent"];

/** What was measured for one agent reply; null = not measured (different from 0 ms). */
export interface ReplyTiming {
  /** Voice: the person stopped talking → the agent started speaking. Text: message sent → reply arrived. */
  responseMs: number | null;
  sttMs: number | null;
  llmMs: number | null;
  ttsMs: number | null;
}

export type TranscriptItem =
  | { kind: "user"; id: string; text: string; interim: string }
  | { kind: "agent"; id: string; text: string; speaking: boolean; timing: ReplyTiming }
  | {
      kind: "tool";
      id: string;
      toolId: string | null;
      name: string;
      knowledge: boolean;
      status: "running" | "done";
      startedAt: number;
      durationMs: number | null;
    }
  | { kind: "step"; id: string; name: string }
  | { kind: "notice"; id: string; tone: "error" | "info"; text: string };

export interface TranscriptState {
  channel: TestChannel;
  items: TranscriptItem[];
  /** Measurements that arrived before the reply they belong to. */
  pending: ReplyTiming;
  /** The current reply's bubble: voice until the agent stops speaking, text until the turn ends. */
  openAgent: string | null;
  /** Voice: when the agent last stopped speaking (sentence text can trail the audio). */
  stoppedAt: number | null;
  /** Text: a reply bubble already exists for the last message. */
  replied: boolean;
  step: string | null;
  seq: number;
}

export type TranscriptAction =
  | { type: "reset"; channel: TestChannel }
  | { type: "voice"; message: FeedbackMessage; at: number }
  | { type: "typed"; text: string }
  | { type: "chat"; event: ChatEvent };

const NO_TIMING: ReplyTiming = { responseMs: null, sttMs: null, llmMs: null, ttsMs: null };
/** Sentence text that arrives this soon after the agent stopped still belongs to that reply. */
export const LATE_TEXT_MS = 1500;

export function emptyTranscript(channel: TestChannel): TranscriptState {
  return { channel, items: [], pending: NO_TIMING, openAgent: null, stoppedAt: null, replied: false, step: null, seq: 0 };
}

// ------------------------------------------------------------------ helpers

function seconds(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? Math.round(value * 1000) : null;
}

function text(value: unknown): string {
  return typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "";
}

/** Same rule as the API's latency parser: `kind`, else the processor name's suffix (never a substring test). */
export function ttfbKind(payload: Record<string, unknown>): "stt" | "llm" | "tts" {
  const kind = typeof payload.kind === "string" ? payload.kind.toLowerCase() : "";
  if (kind === "stt" || kind === "llm" || kind === "tts") return kind;
  const name = String(payload.processor ?? "").trim().replace(/#\d+$/, "").toUpperCase();
  if (name.endsWith("STTSERVICE")) return "stt";
  if (name.endsWith("TTSSERVICE")) return "tts";
  return "llm";
}

const TIMING_KEY = { stt: "sttMs", llm: "llmMs", tts: "ttsMs" } as const;

function join(a: string, b: string, sep: string): string {
  return a ? (b ? `${a}${sep}${b}` : a) : b;
}

function nextId(state: TranscriptState, prefix: string): [string, TranscriptState] {
  return [`${prefix}-${state.seq}`, { ...state, seq: state.seq + 1 }];
}

function update(state: TranscriptState, id: string, fn: (item: TranscriptItem) => TranscriptItem): TranscriptState {
  return { ...state, items: state.items.map((item) => (item.id === id ? fn(item) : item)) };
}

function findAgent(state: TranscriptState, id: string | null): Extract<TranscriptItem, { kind: "agent" }> | null {
  const item = id ? state.items.find((i) => i.id === id) : undefined;
  return item?.kind === "agent" ? item : null;
}

function addAgent(state: TranscriptState, body: string, speaking: boolean, timing: ReplyTiming): TranscriptState {
  const [id, s] = nextId(state, "agent");
  return { ...s, items: [...s.items, { kind: "agent", id, text: body, speaking, timing }], openAgent: id, pending: NO_TIMING };
}

function appendAgent(state: TranscriptState, id: string, body: string, sep: string): TranscriptState {
  return update(state, id, (item) => (item.kind === "agent" ? { ...item, text: join(item.text, body, sep) } : item));
}

function addStep(state: TranscriptState, name: string): TranscriptState {
  if (!name || name === state.step) return state;
  const [id, s] = nextId(state, "step");
  return { ...s, step: name, items: [...s.items, { kind: "step", id, name }] };
}

function addNotice(state: TranscriptState, tone: "error" | "info", body: string): TranscriptState {
  const [id, s] = nextId(state, "notice");
  return { ...s, items: [...s.items, { kind: "notice", id, tone, text: body }] };
}

function startTool(state: TranscriptState, toolId: string | null, name: string, knowledge: boolean, at: number): TranscriptState {
  if (toolId && state.items.some((i) => i.kind === "tool" && i.toolId === toolId)) return state;
  const [id, s] = nextId(state, "tool");
  // The agent's words after a tool belong in a new bubble below its card.
  return {
    ...s,
    openAgent: null,
    items: [...s.items, { kind: "tool", id, toolId, name, knowledge, status: "running", startedAt: at, durationMs: null }],
  };
}

function endTool(state: TranscriptState, toolId: string | null, name: string, knowledge: boolean, at: number): TranscriptState {
  const match = [...state.items]
    .reverse()
    .find((i) => i.kind === "tool" && (toolId ? i.toolId === toolId : i.status === "running" && i.name === name));
  if (match?.kind === "tool") {
    if (match.status === "done") return state;
    return update(state, match.id, (item) =>
      item.kind === "tool" ? { ...item, status: "done", durationMs: Math.max(0, Math.round(at - item.startedAt)) } : item,
    );
  }
  // Its start was missed (e.g. the transcript opened mid-call): shown done, duration unknown.
  const [id, s] = nextId(state, "tool");
  return {
    ...s,
    openAgent: null,
    items: [...s.items, { kind: "tool", id, toolId, name, knowledge, status: "done", startedAt: at, durationMs: null }],
  };
}

const KNOWLEDGE_TOOL = "retrieve_from_knowledge_base";

// ------------------------------------------------------------------ voice

/** Streaming recognition can split "I'm" into "I" + "'m": no space before the second part. */
const CONTRACTION = /^['’]/;

function onUserSpeech(state: TranscriptState, payload: Record<string, unknown>): TranscriptState {
  const said = text(payload.text);
  if (!said) return state;
  const final = payload.final !== false;
  const last = state.items[state.items.length - 1];
  if (last?.kind === "user") {
    // Interim text is replaced by the next interim or by the final; finals of one turn join.
    return update(state, last.id, (item) =>
      item.kind === "user" ? (final ? { ...item, text: join(item.text, said, CONTRACTION.test(said) ? "" : " "), interim: "" } : { ...item, interim: said }) : item,
    );
  }
  const [id, s] = nextId(state, "user");
  return { ...s, items: [...s.items, { kind: "user", id, text: final ? said : "", interim: final ? "" : said }] };
}

function onVoice(state: TranscriptState, message: FeedbackMessage, at: number): TranscriptState {
  const p = message.payload && typeof message.payload === "object" ? message.payload : {};
  switch (message.type) {
    case "rtf-user-transcription":
      return onUserSpeech(state, p);

    case "rtf-bot-started-speaking": {
      const open = findAgent(state, state.openAgent);
      if (open) return update(state, open.id, (item) => (item.kind === "agent" ? { ...item, speaking: true } : item));
      return addAgent(state, "", true, state.pending);
    }

    case "rtf-bot-text": {
      const said = text(p.text);
      if (!said) return state;
      const open = findAgent(state, state.openAgent);
      if (open) return appendAgent(state, open.id, said, " ");
      // Sentence text can trail the audio slightly: keep it with the reply that just ended.
      const last = state.items[state.items.length - 1];
      if (last?.kind === "agent" && state.stoppedAt !== null && at - state.stoppedAt <= LATE_TEXT_MS) {
        return appendAgent(state, last.id, said, " ");
      }
      // Text before the agent started speaking opens the reply; the start marks it speaking.
      return addAgent(state, said, false, state.pending);
    }

    case "rtf-bot-stopped-speaking":
      return {
        ...state,
        openAgent: null,
        stoppedAt: at,
        items: state.items.map((item) => (item.kind === "agent" && item.speaking ? { ...item, speaking: false } : item)),
      };

    case "rtf-latency-measured": {
      const ms = seconds(p.latency_seconds);
      if (ms === null) return state;
      const open = findAgent(state, state.openAgent);
      if (open && open.timing.responseMs === null) {
        return update(state, open.id, (item) => (item.kind === "agent" ? { ...item, timing: { ...item.timing, responseMs: ms } } : item));
      }
      return state.pending.responseMs === null ? { ...state, pending: { ...state.pending, responseMs: ms } } : state;
    }

    case "rtf-ttfb-metric": {
      const ms = seconds(p.ttfb_seconds);
      if (ms === null) return state;
      const kind = ttfbKind(p);
      const key = TIMING_KEY[kind];
      const open = findAgent(state, state.openAgent);
      // Thinking and voice measurements during a reply belong to it (first one wins);
      // speech recognition while the agent talks is already the next turn.
      if (open && kind !== "stt") {
        return open.timing[key] === null
          ? update(state, open.id, (item) => (item.kind === "agent" ? { ...item, timing: { ...item.timing, [key]: ms } } : item))
          : state;
      }
      return state.pending[key] === null ? { ...state, pending: { ...state.pending, [key]: ms } } : state;
    }

    case "rtf-function-call-start": {
      const name = text(p.function_name) || "tool";
      return startTool(state, text(p.tool_call_id) || null, name, name === KNOWLEDGE_TOOL, at);
    }

    case "rtf-function-call-end": {
      const name = text(p.function_name) || "tool";
      return endTool(state, text(p.tool_call_id) || null, name, name === KNOWLEDGE_TOOL, at);
    }

    case "rtf-node-transition":
      return addStep(state, text(p.node_name) || text(message.node_name));

    case "rtf-pipeline-error":
      return addNotice(state, "error", p.fatal === true ? "The agent stopped because of an error." : "The agent ran into an error and carried on.");

    default:
      return state;
  }
}

// ------------------------------------------------------------------ text

function onChat(state: TranscriptState, event: ChatEvent): TranscriptState {
  const at = event.at_ms;
  switch (event.type) {
    case "agent_text": {
      const said = (event.text ?? "").trim();
      if (!said) return state;
      const open = findAgent(state, state.openAgent);
      const last = state.items[state.items.length - 1];
      if (open && last?.id === open.id) return appendAgent(state, open.id, said, "\n\n");
      // The first message after the tester's is the reply: its arrival time is the delay.
      const timing = state.replied ? NO_TIMING : { ...NO_TIMING, responseMs: at };
      return { ...addAgent(state, said, false, timing), replied: true };
    }
    case "tool_start":
      return startTool(state, event.tool_id ?? null, event.name ?? "tool", event.kind === "knowledge", at);
    case "tool_end":
      return endTool(state, event.tool_id ?? null, event.name ?? "tool", event.kind === "knowledge", at);
    case "step":
      return addStep(state, (event.name ?? "").trim());
    case "error":
      return addNotice({ ...state, openAgent: null }, "error", event.message ?? "The agent could not reply.");
    case "done": {
      const s = { ...state, openAgent: null };
      return event.ended ? addNotice(s, "info", "The agent ended the conversation.") : s;
    }
    default:
      return state;
  }
}

function onTyped(state: TranscriptState, typed: string): TranscriptState {
  const said = typed.trim();
  if (!said) return state;
  const [id, s] = nextId(state, "user");
  return { ...s, openAgent: null, replied: false, items: [...s.items, { kind: "user", id, text: said, interim: "" }] };
}

export function transcriptReducer(state: TranscriptState, action: TranscriptAction): TranscriptState {
  switch (action.type) {
    case "reset":
      return emptyTranscript(action.channel);
    case "voice":
      return onVoice(state, action.message, action.at);
    case "typed":
      return onTyped(state, action.text);
    case "chat":
      return onChat(state, action.event);
  }
}

// ------------------------------------------------------------------ display

/** "Knowledge lookup" for the built-in knowledge tool, else the action's name in words. */
export function toolLabel(name: string, knowledge: boolean): string {
  if (knowledge || name === KNOWLEDGE_TOOL) return "Knowledge lookup";
  const words = name.replace(/[_-]+/g, " ").replace(/\s+/g, " ").trim();
  return words ? words[0]!.toUpperCase() + words.slice(1) : "Action";
}

export interface DelayBadge {
  label: string;
  ms: number;
  tone: SummaryTone;
}

/**
 * The figure shown above an agent reply. Voice: its measured response, coloured against
 * the Response times targets (800 ms / 1500 ms). Text: the reply time, neutral (a typed
 * reply is the whole answer, not the first word). Without a response (the opening line)
 * the model's or the voice's first output is shown instead.
 */
export function delayBadge(timing: ReplyTiming, channel: TestChannel): DelayBadge | null {
  if (timing.responseMs !== null) {
    return channel === "text"
      ? { label: "Reply", ms: timing.responseMs, tone: "neutral" }
      : { label: "Response", ms: timing.responseMs, tone: responseTone(timing.responseMs, DEFAULT_THRESHOLDS) };
  }
  if (timing.llmMs !== null) return { label: "Thinking", ms: timing.llmMs, tone: "neutral" };
  if (timing.ttsMs !== null) return { label: "First audio", ms: timing.ttsMs, tone: "neutral" };
  return null;
}

/** Hover detail: each stage's time to its first output, in pipeline order. */
export function timingBreakdown(timing: ReplyTiming): { label: string; ms: number }[] {
  const rows: { label: string; ms: number | null }[] = [
    { label: "Speech recognition", ms: timing.sttMs },
    { label: "Language model, first words", ms: timing.llmMs },
    { label: "Voice, first audio", ms: timing.ttsMs },
  ];
  return rows.filter((r): r is { label: string; ms: number } => r.ms !== null);
}
