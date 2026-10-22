import {
  delayBadge,
  emptyTranscript,
  LATE_TEXT_MS,
  timingBreakdown,
  toolLabel,
  transcriptReducer,
  ttfbKind,
  type ChatEvent,
  type TranscriptAction,
  type TranscriptItem,
  type TranscriptState,
} from "@/features/voice/test-transcript-model";
import type { FeedbackMessage } from "@/lib/webrtc";

type Raw = [at: number, type: string, payload?: Record<string, unknown>];

function voice(events: Raw[], from: TranscriptState = emptyTranscript("voice")): TranscriptState {
  return events.reduce<TranscriptState>((s, [at, type, payload]) => {
    const message: FeedbackMessage = { type, payload: payload ?? {}, node_name: "Conversation" };
    return transcriptReducer(s, { type: "voice", message, at });
  }, from);
}

function chat(actions: (ChatEvent | string)[], from: TranscriptState = emptyTranscript("text")): TranscriptState {
  return actions.reduce<TranscriptState>((s, a) => {
    const action: TranscriptAction = typeof a === "string" ? { type: "typed", text: a } : { type: "chat", event: a };
    return transcriptReducer(s, action);
  }, from);
}

/** A compact view of the bubbles for assertions. */
function view(items: TranscriptItem[]): string[] {
  return items.map((i) => {
    switch (i.kind) {
      case "user":
        return `user: ${i.text}${i.interim ? ` [${i.interim}]` : ""}`;
      case "agent":
        return `agent${i.speaking ? "*" : ""}: ${i.text}`;
      case "tool":
        return `tool: ${toolLabel(i.name, i.knowledge)} ${i.status}${i.durationMs !== null ? ` ${i.durationMs}ms` : ""}`;
      case "step":
        return `step: ${i.name}`;
      case "notice":
        return `${i.tone}: ${i.text}`;
    }
  });
}

const agents = (s: TranscriptState) => s.items.filter((i): i is Extract<TranscriptItem, { kind: "agent" }> => i.kind === "agent");

// Recorded on a real browser test call (infra/scripts/e2e_test_call.py, run of 2026-10-19):
// self-hosted Whisper, qwen2.5 and Pocket TTS. Times are arrival offsets in ms.
const REAL_CALL: Raw[] = [
  [663, "rtf-node-transition", { node_id: "n2", node_name: "Conversation", previous_node_id: null, previous_node_name: null, allow_interrupt: true }],
  [1204, "rtf-ttfb-metric", { ttfb_seconds: 0.537921667098999, processor: "SpeachesTTSService#2", model: "pocket-tts", kind: "tts" }],
  [1215, "rtf-bot-started-speaking", {}],
  [8967, "rtf-bot-text", { text: "Hi Alex, this is Sarah from Demo Realty (demo data). Is now a good time for two quick questions?" }],
  [9048, "rtf-bot-stopped-speaking", {}],
  [14682, "rtf-ttfb-metric", { ttfb_seconds: 1.9966380596160889, processor: "SpeachesSTTService#2", model: "Systran/faster-whisper-tiny.en", kind: "stt" }],
  [14683, "rtf-user-transcription", { text: "Yes, now it's fine. I'm looking to buy my first home", final: true, timestamp: "2026-10-19T06:53:45.692+00:00", user_id: "" }],
  [15117, "rtf-ttfb-metric", { ttfb_seconds: 0.4292416572570801, processor: "SpeachesLLMService#2", model: "qwen2.5:1.5b", kind: "llm" }],
  [16411, "rtf-ttfb-metric", { ttfb_seconds: 0.7506556510925293, processor: "SpeachesTTSService#2", model: "pocket-tts", kind: "tts" }],
  [16451, "rtf-bot-started-speaking", {}],
  [16453, "rtf-latency-measured", { latency_seconds: 3.7653677463531494 }],
  [17611, "rtf-bot-text", { text: "Okay, great." }],
  [20451, "rtf-bot-text", { text: "How many people are in your family if you're buying together?" }],
  [20530, "rtf-bot-stopped-speaking", {}],
];

describe("voice transcript", () => {
  it("turns a real call's event stream into bubbles with each reply's measured delay", () => {
    const s = voice(REAL_CALL);
    expect(view(s.items)).toEqual([
      "step: Conversation",
      "agent: Hi Alex, this is Sarah from Demo Realty (demo data). Is now a good time for two quick questions?",
      "user: Yes, now it's fine. I'm looking to buy my first home",
      "agent: Okay, great. How many people are in your family if you're buying together?",
    ]);
    const [greeting, reply] = agents(s);
    // The opening line has no response time (nobody spoke first): its first audio is shown.
    expect(greeting!.timing).toEqual({ responseMs: null, sttMs: null, llmMs: null, ttsMs: 538 });
    expect(delayBadge(greeting!.timing, "voice")).toEqual({ label: "First audio", ms: 538, tone: "neutral" });
    // The reply carries everything measured for its turn, though it arrived before the bubble.
    expect(reply!.timing).toEqual({ responseMs: 3765, sttMs: 1997, llmMs: 429, ttsMs: 751 });
    expect(delayBadge(reply!.timing, "voice")).toEqual({ label: "Response", ms: 3765, tone: "bad" });
    expect(timingBreakdown(reply!.timing)).toEqual([
      { label: "Speech recognition", ms: 1997 },
      { label: "Language model, first words", ms: 429 },
      { label: "Voice, first audio", ms: 751 },
    ]);
    expect(s.openAgent).toBeNull();
    expect(s.pending).toEqual({ responseMs: null, sttMs: null, llmMs: null, ttsMs: null });
  });

  it("shows the agent speaking until it stops", () => {
    const s = voice(REAL_CALL.slice(0, 4));
    expect(view(s.items).at(-1)).toMatch(/^agent\*: Hi Alex/);
    const started = voice([[0, "rtf-bot-started-speaking"]]);
    expect(view(started.items)).toEqual(["agent*: "]);
  });

  it("replaces interim speech with the final words and joins one turn's finals", () => {
    const s = voice([
      [0, "rtf-user-transcription", { text: "I'm loo", final: false }],
      [10, "rtf-user-transcription", { text: "I'm looking for", final: false }],
    ]);
    expect(view(s.items)).toEqual(["user:  [I'm looking for]"]);
    const final = voice([[20, "rtf-user-transcription", { text: "I'm looking for a house.", final: true }]], s);
    expect(view(final.items)).toEqual(["user: I'm looking for a house."]);
    const more = voice(
      [
        [30, "rtf-user-transcription", { text: "Three", final: false }],
        [40, "rtf-user-transcription", { text: "Three bedrooms.", final: true }],
        [50, "rtf-user-transcription", { text: "   ", final: true }],
      ],
      final,
    );
    expect(view(more.items)).toEqual(["user: I'm looking for a house. Three bedrooms."]);
  });

  it("follows streaming speech recognition: interim words, several finals, one bubble", () => {
    // Recorded on a real browser test call with streaming recognition (2026-10-19, 23:01).
    const stt = (at: number, s: number): Raw => [at, "rtf-ttfb-metric", { ttfb_seconds: s, processor: "DeepgramSTTService#3", model: "zipformer-en-streaming", kind: "stt" }];
    const heard = (at: number, text: string, final: boolean): Raw => [at, "rtf-user-transcription", { text, final, timestamp: "2026-10-19T18:01:54.298+00:00", user_id: "" }];
    const mid = voice([
      heard(13013, "Yes", false),
      heard(13356, "Yes now", false),
      stt(13481, 0.28234267234802246),
      heard(13482, "Yes now is fine", true),
      heard(13521, "I", false),
      heard(13821, "I", true),
      heard(14128, "'m looking", false),
    ]);
    expect(view(mid.items)).toEqual(["user: Yes now is fine I ['m looking]"]);
    const done = voice(
      [
        heard(14778, "'m looking to buy my first", false),
        stt(15021, 0.4073958396911621),
        heard(15030, "'m looking to buy my first home", true),
        [17782, "rtf-ttfb-metric", { ttfb_seconds: 2.3424339294433594, processor: "SpeachesLLMService#18", model: "qwen2.5:1.5b", kind: "llm" }],
        [19219, "rtf-ttfb-metric", { ttfb_seconds: 0.9565036296844482, processor: "SpeachesTTSService#8", model: "pocket-tts", kind: "tts" }],
        [19248, "rtf-bot-started-speaking", {}],
        [19249, "rtf-latency-measured", { latency_seconds: 4.63307785987854 }],
        [21102, "rtf-bot-text", { text: "That sounds exciting!" }],
      ],
      mid,
    );
    expect(view(done.items)).toEqual(["user: Yes now is fine I'm looking to buy my first home", "agent*: That sounds exciting!"]);
    // The turn's first recognition result is its speech-recognition time, as on the call's record.
    expect(agents(done)[0]!.timing).toEqual({ responseMs: 4633, sttMs: 282, llmMs: 2342, ttsMs: 957 });
  });

  it("keeps out-of-order agent text with the reply it belongs to", () => {
    // Text before the agent started speaking opens the reply; the start does not add one.
    const early = voice([
      [0, "rtf-bot-text", { text: "Sure." }],
      [5, "rtf-bot-started-speaking"],
      [9, "rtf-bot-text", { text: "One moment." }],
    ]);
    expect(view(early.items)).toEqual(["agent*: Sure. One moment."]);
    // Sentence text trailing the audio stays in the same bubble.
    const stopped = voice([[100, "rtf-bot-stopped-speaking"]], early);
    const late = voice([[100 + LATE_TEXT_MS - 1, "rtf-bot-text", { text: "Ready?" }]], stopped);
    expect(view(late.items)).toEqual(["agent: Sure. One moment. Ready?"]);
    // Much later, without the caller saying anything, it is a new reply.
    const next = voice([[100 + LATE_TEXT_MS + 5000, "rtf-bot-text", { text: "Are you still there?" }]], late);
    expect(view(next.items)).toEqual(["agent: Sure. One moment. Ready?", "agent: Are you still there?"]);
  });

  it("picks out sound cues in the agent's words as they arrive", () => {
    const s = voice([
      [0, "rtf-bot-started-speaking"],
      [5, "rtf-bot-text", { text: "[laugh] That's a good one." }],
      [9, "rtf-bot-text", { text: "Anything [Sighs] else?" }],
    ]);
    const reply = agents(s)[0]!;
    expect(reply.text).toBe("[laugh] That's a good one. Anything [Sighs] else?");
    expect(reply.parts).toEqual([
      { kind: "cue", tag: "laugh", label: "laughs" },
      { kind: "text", text: " That's a good one. Anything " },
      { kind: "cue", tag: "sigh", label: "sighs" },
      { kind: "text", text: " else?" },
    ]);
    // Typed chats too; other bracketed text stays as written.
    const typed = chat(["Hi", { type: "agent_text", text: "Ha [chuckle], press [1].", at_ms: 300 } as ChatEvent]);
    expect(agents(typed)[0]!.parts.map((p) => (p.kind === "cue" ? `(${p.label})` : p.text)).join("")).toBe("Ha (chuckles), press [1].");
    expect(agents(voice([[0, "rtf-bot-started-speaking"]]))[0]!.parts).toEqual([]);
  });

  it("colours the measured response by the Response times targets", () => {
    const reply = (latency: number) =>
      agents(voice([[0, "rtf-latency-measured", { latency_seconds: latency }], [1, "rtf-bot-started-speaking"]]))[0]!;
    expect(delayBadge(reply(0.7).timing, "voice")?.tone).toBe("good");
    expect(delayBadge(reply(0.8).timing, "voice")?.tone).toBe("good");
    expect(delayBadge(reply(1.2).timing, "voice")?.tone).toBe("warn");
    expect(delayBadge(reply(1.5).timing, "voice")).toEqual({ label: "Response", ms: 1500, tone: "bad" });
    // Invalid figures are ignored rather than shown as 0 ms.
    const bad = voice([[0, "rtf-latency-measured", { latency_seconds: -1 }], [1, "rtf-ttfb-metric", { ttfb_seconds: "x" }], [2, "rtf-bot-started-speaking"]]);
    expect(delayBadge(agents(bad)[0]!.timing, "voice")).toBeNull();
  });

  it("gives a reply only the first measurement of each stage and leaves the next turn's for later", () => {
    const s = voice([
      [0, "rtf-bot-started-speaking"],
      [1, "rtf-ttfb-metric", { ttfb_seconds: 0.3, kind: "tts" }],
      [2, "rtf-ttfb-metric", { ttfb_seconds: 0.9, kind: "tts" }],
      [3, "rtf-ttfb-metric", { ttfb_seconds: 0.4, processor: "DeepgramSTTService#0" }], // barge-in: next turn
      [4, "rtf-bot-stopped-speaking"],
      [5, "rtf-user-transcription", { text: "Wait", final: true }],
      [6, "rtf-bot-started-speaking"],
    ]);
    const [first, second] = agents(s);
    expect(first!.timing).toEqual({ responseMs: null, sttMs: null, llmMs: null, ttsMs: 300 });
    expect(second!.timing.sttMs).toBe(400);
  });

  it("shows tools as cards with their duration and steps as dividers", () => {
    const s = voice([
      [0, "rtf-bot-started-speaking"],
      [10, "rtf-bot-text", { text: "Let me check." }],
      [20, "rtf-function-call-start", { function_name: "retrieve_from_knowledge_base", tool_call_id: "c1", arguments: { q: "x" } }],
      [25, "rtf-function-call-start", { function_name: "retrieve_from_knowledge_base", tool_call_id: "c1" }],
      [440, "rtf-function-call-end", { function_name: "retrieve_from_knowledge_base", tool_call_id: "c1", result: "secret" }],
      [450, "rtf-bot-text", { text: "It costs 49 dollars." }],
      [460, "rtf-node-transition", { node_name: "Qualify" }],
      [461, "rtf-node-transition", { node_name: "Qualify" }],
      [470, "rtf-function-call-end", { function_name: "book_meeting", tool_call_id: "c2" }],
      [480, "rtf-pipeline-error", { error: "OpenAILLMService#1: 401 invalid api key", fatal: false }],
    ]);
    expect(view(s.items)).toEqual([
      "agent*: Let me check.",
      "tool: Knowledge lookup done 420ms",
      "agent: It costs 49 dollars.",
      "step: Qualify",
      "tool: Book meeting done",
      "error: The agent ran into an error and carried on.",
    ]);
    expect(JSON.stringify(s.items)).not.toContain("invalid api key");
  });

  it("classifies first-byte measurements by kind, else by the processor's suffix", () => {
    expect(ttfbKind({ kind: "TTS" })).toBe("tts");
    expect(ttfbKind({ processor: "DeepgramSTTService#3" })).toBe("stt");
    expect(ttfbKind({ processor: "ElevenLabsTTSService" })).toBe("tts");
    expect(ttfbKind({ processor: "OpenAILLMService#0" })).toBe("llm");
    expect(ttfbKind({})).toBe("llm");
  });
});

describe("text transcript", () => {
  const ev = (e: Partial<ChatEvent> & Pick<ChatEvent, "type" | "at_ms">): ChatEvent => e as ChatEvent;

  it("streams the greeting and each reply with the time it took", () => {
    const s = chat([
      ev({ type: "started", at_ms: 3, call_id: "c1" }),
      ev({ type: "step", at_ms: 40, name: "Conversation" }),
      ev({ type: "agent_text", at_ms: 1180, text: "Hi Alex, this is Sarah." }),
      ev({ type: "done", at_ms: 1190, ended: false }),
      "How much is it?",
      ev({ type: "tool_start", at_ms: 300, tool_id: "t1", name: "retrieve_from_knowledge_base", kind: "knowledge" }),
      ev({ type: "tool_end", at_ms: 720, tool_id: "t1", name: "retrieve_from_knowledge_base", kind: "knowledge" }),
      ev({ type: "agent_text", at_ms: 2100, text: "Plans start at 49 dollars." }),
      ev({ type: "agent_text", at_ms: 2150, text: "Want details?" }),
      ev({ type: "done", at_ms: 2160, ended: false }),
    ]);
    expect(view(s.items)).toEqual([
      "step: Conversation",
      "agent: Hi Alex, this is Sarah.",
      "user: How much is it?",
      "tool: Knowledge lookup done 420ms",
      "agent: Plans start at 49 dollars.\n\nWant details?",
    ]);
    const [greeting, reply] = agents(s);
    expect(delayBadge(greeting!.timing, "text")).toEqual({ label: "Reply", ms: 1180, tone: "neutral" });
    expect(reply!.timing.responseMs).toBe(2100);
  });

  it("reports failed turns and the agent ending the chat in plain words", () => {
    const s = chat([
      "Hello?",
      ev({ type: "error", at_ms: 50, message: "The agent could not finish its reply. Try sending your message again." }),
      "Bye",
      ev({ type: "agent_text", at_ms: 900, text: "Goodbye!" }),
      ev({ type: "done", at_ms: 950, ended: true }),
      "   ",
    ]);
    expect(view(s.items)).toEqual([
      "user: Hello?",
      "error: The agent could not finish its reply. Try sending your message again.",
      "user: Bye",
      "agent: Goodbye!",
      "info: The agent ended the conversation.",
    ]);
  });

  it("starts over on reset", () => {
    const s = chat(["Hi"]);
    expect(transcriptReducer(s, { type: "reset", channel: "voice" })).toEqual(emptyTranscript("voice"));
  });
});

describe("tool labels", () => {
  it("names the knowledge tool and turns action names into words", () => {
    expect(toolLabel("retrieve_from_knowledge_base", false)).toBe("Knowledge lookup");
    expect(toolLabel("anything", true)).toBe("Knowledge lookup");
    expect(toolLabel("book_meeting", false)).toBe("Book meeting");
    expect(toolLabel("send-quote", false)).toBe("Send quote");
    expect(toolLabel("__", false)).toBe("Action");
  });
});
