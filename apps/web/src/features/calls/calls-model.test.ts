import { waitFor } from "@testing-library/react";

import {
  blockToQuery,
  createCallsDatasource,
  filtersActive,
  flattenUsage,
  outcomeLabel,
  outcomeTone,
  parseTranscript,
  presetRange,
  readCallsUrl,
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

  it("flattens usage objects two levels deep", () => {
    expect(flattenUsage({ call_duration_seconds: 42.123, llm: { "gpt-4.1-mini": { prompt_tokens: 900 } }, stt: {}, note: null })).toEqual([
      { key: "call_duration_seconds", value: "42.123" },
      { key: "llm.gpt-4.1-mini", value: '{"prompt_tokens":900}' },
      { key: "note", value: "—" },
    ]);
  });
});

describe("parseTranscript", () => {
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
