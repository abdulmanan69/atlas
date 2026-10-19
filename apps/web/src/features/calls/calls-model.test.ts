import { waitFor } from "@testing-library/react";

import {
  blockToQuery,
  createCallsDatasource,
  directionLabel,
  filtersActive,
  formatOffset,
  outcomeLabel,
  outcomeTone,
  parseTranscript,
  presetRange,
  readCallsUrl,
  summarizeUsage,
  urlToFilters,
  type CallPage,
} from "@/features/calls/calls-model";
import { formatDuration } from "@/lib/format";

describe("URL state → API filters", () => {
  it("reads every filter from the URL and drops invalid values", () => {
    const u = readCallsUrl(
      new URLSearchParams({
        from: "2026-10-01T00:00:00.000Z",
        to: "not-a-date",
        agent: "a1",
        campaign: "c1",
        state: "completed",
        outcome: "qualified",
        mode: "simulated",
        direction: "sideways",
      }),
    );
    expect(u).toEqual({
      from: "2026-10-01T00:00:00.000Z",
      to: null,
      agent: "a1",
      campaign: "c1",
      lead: null,
      state: "completed",
      outcome: "qualified",
      mode: "simulated",
      direction: null,
    });
    expect(urlToFilters(u)).toEqual({
      created_from: "2026-10-01T00:00:00.000Z",
      agent_id: "a1",
      campaign_id: "c1",
      state: "completed",
      outcome: "qualified",
      mode: "simulated",
    });
  });

  it("sends nothing for an empty URL and treats mode=all as no filter", () => {
    const u = readCallsUrl(new URLSearchParams({ mode: "all", state: "bogus" }));
    expect(u.mode).toBe("all");
    expect(u.state).toBeNull();
    expect(urlToFilters(u)).toEqual({});
    expect(filtersActive(u)).toBe(false);
    expect(filtersActive({ ...u, mode: "live" })).toBe(true);
    expect(filtersActive({ ...u, lead: "l1" })).toBe(true);
  });

  it("filters browser test calls and labels them by where they came from", () => {
    const u = readCallsUrl(new URLSearchParams({ mode: "test" }));
    expect(u.mode).toBe("test");
    expect(urlToFilters(u)).toEqual({ mode: "test" });
    expect(filtersActive(u)).toBe(true);
    expect(readCallsUrl(new URLSearchParams({ mode: "loud" })).mode).toBe("all");
    expect(directionLabel({ mode: "test", direction: "inbound" })).toBe("Browser");
    expect(directionLabel({ mode: "live", direction: "inbound" })).toBe("Inbound");
    expect(directionLabel({ mode: "simulated", direction: "outbound" })).toBe("Outbound");
  });

  it("builds local-day preset ranges", () => {
    const now = new Date(2026, 9, 18, 15, 30);
    expect(presetRange("today", now)).toEqual({ from: new Date(2026, 9, 18).toISOString(), to: null });
    expect(presetRange("yesterday", now)).toEqual({ from: new Date(2026, 9, 17).toISOString(), to: new Date(2026, 9, 18).toISOString() });
    expect(presetRange("7d", now).from).toBe(new Date(2026, 9, 12).toISOString());
    expect(presetRange("30d", now).from).toBe(new Date(2026, 8, 19).toISOString());
    expect(presetRange("month", now).from).toBe(new Date(2026, 9, 1).toISOString());
  });
});

describe("grid mapping", () => {
  it("turns an infinite-model block into a page request", () => {
    expect(blockToQuery({ startRow: 0, endRow: 50 }, { state: "failed" })).toEqual({ state: "failed", page: 1, page_size: 50 });
    expect(blockToQuery({ startRow: 100, endRow: 150 }, {})).toMatchObject({ page: 3, page_size: 50 });
  });

  it("fetches the block with current filters and reports the total", async () => {
    const page: CallPage = { items: [], total: 3 };
    const fetchPage = vi.fn(async () => page);
    const onResult = vi.fn();
    const ds = createCallsDatasource({ fetchPage, getFilters: () => ({ mode: "live" }), onResult });
    const params = { startRow: 0, endRow: 50, sortModel: [], filterModel: {}, successCallback: vi.fn(), failCallback: vi.fn(), context: undefined, api: undefined as never };
    ds.getRows(params);
    await waitFor(() => expect(params.successCallback).toHaveBeenCalledWith([], 3));
    expect(fetchPage).toHaveBeenCalledWith({ mode: "live", page: 1, page_size: 50 });
    expect(onResult).toHaveBeenCalledWith(page);
  });

  it("fails the block and surfaces the error", async () => {
    const error = new Error("boom");
    const onError = vi.fn();
    const ds = createCallsDatasource({ fetchPage: () => Promise.reject(error), getFilters: () => ({}), onError });
    const params = { startRow: 0, endRow: 50, sortModel: [], filterModel: {}, successCallback: vi.fn(), failCallback: vi.fn(), context: undefined, api: undefined as never };
    ds.getRows(params);
    await waitFor(() => expect(params.failCallback).toHaveBeenCalled());
    expect(onError).toHaveBeenCalledWith(error);
  });
});

describe("badges and formatting", () => {
  it("maps outcome codes to tones and readable labels", () => {
    expect(outcomeTone("qualified")).toBe("success");
    expect(outcomeTone("do_not_call")).toBe("danger");
    expect(outcomeTone("something_custom")).toBe("neutral");
    expect(outcomeTone(null)).toBe("neutral");
    expect(outcomeLabel("appointment_booked")).toBe("Appointment booked");
    expect(outcomeLabel(null)).toBe("—");
  });

  it("formats durations as m:ss, with hours when needed, and null as unknown", () => {
    expect(formatDuration(0)).toBe("0:00");
    expect(formatDuration(61)).toBe("1:01");
    expect(formatDuration(3725)).toBe("1:02:05");
    expect(formatDuration(null)).toBe("—");
    expect(formatDuration(undefined)).toBe("—");
  });

  it("summarises voice-service usage per model without internal names", () => {
    const usage = {
      llm: {
        "SpeachesLLMService#2|||qwen2.5:1.5b": {
          total_tokens: 972,
          prompt_tokens: 903,
          completion_tokens: 69,
          cache_read_input_tokens: 888,
          reasoning_tokens: null,
        },
      },
      stt: { "SpeachesSTTService|||Systran/faster-whisper-base": 15.739 },
      tts: { "SpeachesTTSService|||pocket-tts": 604 },
      call_duration_seconds: 64,
    };
    const { rows, selfHostedOnly } = summarizeUsage(usage);
    expect(selfHostedOnly).toBe(true);
    expect(rows).toEqual([
      { label: "Language model", model: "qwen2.5:1.5b", value: "972 tokens (903 in, 69 out, 888 reused from cache)" },
      { label: "Speech-to-text", model: "Systran/faster-whisper-base", value: "15.7 s of audio transcribed" },
      { label: "Voice", model: "pocket-tts", value: "604 characters spoken" },
    ]);
    expect(rows.some((r) => /Service|\|\|\|/.test(`${r.label}${r.model}${r.value}`))).toBe(false);
  });

  it("marks hosted usage as chargeable and keeps unknown keys readable", () => {
    const { rows, selfHostedOnly } = summarizeUsage({
      llm: { "OpenAILLMService#1|||gpt-4.1-mini": { prompt_tokens: 900 } },
      extra_minutes: 2,
    });
    expect(selfHostedOnly).toBe(false);
    expect(rows).toEqual([
      { label: "Language model", model: "gpt-4.1-mini", value: "900 tokens (900 in)" },
      { label: "Extra minutes", model: null, value: "2" },
    ]);
    expect(summarizeUsage(null)).toEqual({ rows: [], selfHostedOnly: false });
  });
});

describe("parseTranscript", () => {
  it("reads voice-service transcripts: time order, offsets, roles and repeated lines", () => {
    const text = [
      "[2026-10-19T16:50:35.758+00:00] assistant: Hi Alex, this is Sarah.",
      "[2026-10-19T16:50:50.565+00:00 → 2026-10-19T16:50:52.894+00:00] user: Yes.",
      "[2026-10-19T16:51:09.448+00:00 → 2026-10-19T16:51:14.829+00:00] user: Something else",
      "Something else",
      // The service wrote this reply before the words it answered.
      "[2026-10-19T16:51:31.261+00:00 → 2026-10-19T16:51:31.840+00:00] assistant: Got it. How long is the timeline?",
      "[2026-10-19T16:51:23.366+00:00 → 2026-10-19T16:51:29.850+00:00] user: for years.",
      "In this, I do not plan to do it.",
    ].join("\n");
    const parsed = parseTranscript(text);
    expect(parsed.kind).toBe("turns");
    if (parsed.kind !== "turns") return;
    expect(parsed.turns.map((t) => [t.role, t.speaker, t.text, t.offset !== null && t.offset !== undefined ? formatOffset(t.offset) : null])).toEqual([
      ["agent", "Agent", "Hi Alex, this is Sarah.", "0:00"],
      ["person", "Caller", "Yes.", "0:14"],
      ["person", "Caller", "Something else", "0:33"],
      ["person", "Caller", "for years.\nIn this, I do not plan to do it.", "0:47"],
      ["agent", "Agent", "Got it. How long is the timeline?", "0:55"],
    ]);
  });

  it("splits Agent:/Name: lines into turns and keeps markers as notes", () => {
    const parsed = parseTranscript("[SIMULATED CALL]\nAgent: Hi Hina, is now a good time?\nHina: Sure.\nIt is quiet here.\nAgent: Great.\n");
    expect(parsed.kind).toBe("turns");
    if (parsed.kind !== "turns") return;
    expect(parsed.turns).toEqual([
      { role: "note", speaker: null, text: "[SIMULATED CALL]" },
      { role: "agent", speaker: "Agent", text: "Hi Hina, is now a good time?" },
      { role: "person", speaker: "Hina", text: "Sure.\nIt is quiet here." },
      { role: "agent", speaker: "Agent", text: "Great." },
    ]);
  });

  it("leaves text without an Agent: prefix as plain", () => {
    expect(parseTranscript("00:01 hello\n00:02 world")).toEqual({ kind: "plain", text: "00:01 hello\n00:02 world" });
  });
});
