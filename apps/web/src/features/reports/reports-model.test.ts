import type { CallsOverview } from "@/features/queries";
import { byAgentCsv, dailyPoints, METRICS, rate, toCsv } from "@/features/reports/reports-model";

const empty: CallsOverview = {
  range_days: 30,
  totals: { calls: 0, answered: 0, no_answer: 0, busy: 0, failed: 0, canceled: 0, in_progress: 0, attempted: 0, qualified: 0 },
  answer_rate: null,
  qualification_rate: null,
  avg_duration_seconds: null,
  total_duration_seconds: 0,
  estimated_cost: null,
  opted_out: 0,
  daily: [],
  outcomes: [],
  by_agent: [],
  by_campaign: [],
  mode_split: { live: 0, simulated: 0, test: 0 },
};

const busy: CallsOverview = {
  ...empty,
  totals: { ...empty.totals, calls: 8, answered: 3, no_answer: 2, attempted: 7, qualified: 0 },
  answer_rate: 3 / 7,
  qualification_rate: 0,
  avg_duration_seconds: 60,
  estimated_cost: 0.6,
  daily: [
    { date: "2026-10-17", attempted: 4, answered: 1 },
    { date: "2026-10-18", attempted: 3, answered: 2 },
  ],
  by_agent: [
    { agent_id: "a1", agent_name: "Qualifier, \"Q\"", attempted: 4, answered: 3, qualified: 1, avg_duration_seconds: 60, response_p50_ms: 720 },
    { agent_id: null, agent_name: null, attempted: 1, answered: 0, qualified: 0, avg_duration_seconds: null },
  ],
};

const metric = (id: string) => METRICS.find((m) => m.id === id)!;

describe("metric formatting: null is not zero", () => {
  it("shows a dash and flags undefined metrics when nothing was attempted", () => {
    for (const id of ["answer_rate", "qualification_rate", "avg_duration", "estimated_cost"]) {
      expect(metric(id).value(empty)).toBe("—");
      expect(metric(id).undefinedFor?.(empty)).toBe(true);
    }
    expect(metric("attempted").value(empty)).toBe("0");
    expect(metric("answered").value(empty)).toBe("0");
  });

  it("shows a real zero as 0% once the denominator exists", () => {
    expect(metric("answer_rate").value(busy)).toBe("42.9%");
    expect(metric("qualification_rate").value(busy)).toBe("0%");
    expect(metric("qualification_rate").undefinedFor?.(busy)).toBe(false);
    expect(metric("avg_duration").value(busy)).toBe("1:00");
    expect(metric("estimated_cost").value(busy)).toBe("0.60");
  });

  it("every metric carries a definition for its tooltip", () => {
    expect(METRICS).toHaveLength(8);
    for (const m of METRICS) expect(m.definition.length).toBeGreaterThan(20);
  });

  it("response times are undefined until a call is measured, and name their denominator", () => {
    expect(metric("response_p50").value(empty)).toBe("—");
    expect(metric("response_p50").undefinedFor?.(empty)).toBe(true);
    expect(metric("response_p95").note?.(empty)).toBe("No measured calls yet");
    const measured = { ...busy, response_p50_ms: 640, response_p95_ms: 1820, measured_calls: 3 };
    expect(metric("response_p50").value(measured)).toBe("640 ms");
    expect(metric("response_p95").value(measured)).toBe("1.8 s");
    expect(metric("response_p50").note?.(measured)).toBe("Over 3 measured calls");
  });

  it("rate() is null without a denominator", () => {
    expect(rate(0, 0)).toBeNull();
    expect(rate(0, 4)).toBe(0);
    expect(rate(1, 4)).toBe(0.25);
  });
});

describe("daily series", () => {
  it("splits attempted into answered and the remainder so bars stack to attempted", () => {
    expect(dailyPoints(busy.daily)).toEqual([
      { date: "2026-10-17", answered: 1, unanswered: 3, attempted: 4 },
      { date: "2026-10-18", answered: 2, unanswered: 1, attempted: 3 },
    ]);
  });
});

describe("CSV export", () => {
  it("quotes commas and double quotes and ends lines with CRLF", () => {
    expect(toCsv(["a", "b"], [["x,y", 'say "hi"'], [null, 2]])).toBe('a,b\r\n"x,y","say ""hi"""\r\n,2\r\n');
  });

  it("exports the by-agent table with ratios and blanks for undefined rates", () => {
    const lines = byAgentCsv(busy.by_agent).trim().split("\r\n");
    expect(lines[0]).toBe("agent_id,agent,attempted,answered,answer_rate,qualified,qualification_rate,avg_duration_seconds,response_p50_ms");
    expect(lines[1]).toBe('a1,"Qualifier, ""Q""",4,3,0.7500,1,0.3333,60,720');
    expect(lines[2]).toBe(",No agent,1,0,0.0000,0,,,");
  });
});
