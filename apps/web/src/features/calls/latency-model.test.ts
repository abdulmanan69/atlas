import { formatMs, isSlow, pct, responseTone, summaryItems, turnExtent, turnSegments, waterfallScale } from "@/features/calls/latency-model";
import type { LatencySummary, LatencyTurn } from "@/features/calls/latency-types";

const turn = (over: Partial<LatencyTurn> = {}): LatencyTurn => ({
  turn: 1,
  node_name: "Qualify",
  user_text: "Yes",
  agent_text: "Great",
  user_started_at: null,
  user_ended_at: null,
  agent_started_at: null,
  agent_ended_at: null,
  response_ms: 900,
  stt_ttfb_ms: 150,
  llm_ttfb_ms: 400,
  tts_ttfb_ms: 200,
  tools: [],
  slow: false,
  ...over,
});

describe("formatMs", () => {
  it("formats milliseconds, seconds and missing values", () => {
    expect(formatMs(null)).toBe("—");
    expect(formatMs(undefined)).toBe("—");
    expect(formatMs(0)).toBe("0 ms");
    expect(formatMs(419.6)).toBe("420 ms");
    expect(formatMs(1234)).toBe("1.2 s");
    expect(formatMs(1000)).toBe("1.0 s");
    expect(formatMs(14_400)).toBe("14 s");
  });
});

describe("responseTone", () => {
  it("is good under target, warn between, bad from slow", () => {
    expect(responseTone(800)).toBe("good");
    expect(responseTone(1200)).toBe("warn");
    expect(responseTone(1500)).toBe("bad");
    expect(responseTone(null)).toBe("neutral");
  });
});

describe("turnSegments", () => {
  it("lays stages end to end and puts the unexplained rest last", () => {
    expect(turnSegments(turn())).toEqual([
      { kind: "heard", label: "Heard", ms: 150, offset: 0 },
      { kind: "thinking", label: "Thinking", ms: 400, offset: 150 },
      { kind: "speaking", label: "Speaking", ms: 200, offset: 550 },
      { kind: "other", label: "Other", ms: 150, offset: 750 },
    ]);
  });

  it("labels tool spans by kind and skips missing stages", () => {
    const segs = turnSegments(
      turn({
        stt_ttfb_ms: null,
        response_ms: 1000,
        tools: [
          { name: "retrieve_from_knowledge_base", kind: "knowledge", duration_ms: 300 },
          { name: "book_meeting", kind: "action", duration_ms: 100 },
          { name: "noop", kind: "action", duration_ms: null },
        ],
      }),
    );
    expect(segs.map((s) => s.label)).toEqual(["Thinking", "Knowledge lookup", "book_meeting", "Speaking"]);
    expect(segs.map((s) => s.offset)).toEqual([0, 400, 700, 800]);
  });

  it("adds no remainder when stages already exceed the measured response", () => {
    const segs = turnSegments(turn({ response_ms: 500 }));
    expect(segs.some((s) => s.kind === "other")).toBe(false);
    expect(turnExtent(turn({ response_ms: 500 }))).toBe(750);
  });
});

describe("waterfall scale", () => {
  it("is at least the slow threshold and rounds up to 500 ms", () => {
    expect(waterfallScale([turn()])).toBe(1500);
    expect(waterfallScale([turn({ response_ms: 2100 })])).toBe(2500);
    expect(waterfallScale([])).toBe(1500);
  });

  it("clamps percentages", () => {
    expect(pct(750, 1500)).toBe(50);
    expect(pct(3000, 1500)).toBe(100);
    expect(pct(10, 0)).toBe(0);
  });

  it("flags slow turns from the server or the threshold", () => {
    expect(isSlow(turn())).toBe(false);
    expect(isSlow(turn({ slow: true }))).toBe(true);
    expect(isSlow(turn({ response_ms: 1600 }))).toBe(true);
  });
});

describe("summaryItems", () => {
  it("gives every tile a plain definition", () => {
    const s: LatencySummary = {
      source: "runtime",
      turns: 6,
      measured_turns: 5,
      response_p50_ms: 640,
      response_p95_ms: 1800,
      response_max_ms: 2100,
      first_response_ms: null,
      stt_ttfb_p50_ms: 120,
      llm_ttfb_p50_ms: 380,
      tts_ttfb_p50_ms: 150,
      tool_time_ms: 450,
      slow_turns: 1,
      interruptions: 2,
    };
    const items = summaryItems(s);
    expect(items.map((i) => [i.label, i.value])).toEqual([
      ["Median response", "640 ms"],
      ["p95 response", "1.8 s"],
      ["Slowest", "2.1 s"],
      ["First response", "—"],
      ["Interruptions", "2"],
      ["Tool time", "450 ms"],
    ]);
    for (const i of items) expect(i.definition.length).toBeGreaterThan(20);
  });
});
