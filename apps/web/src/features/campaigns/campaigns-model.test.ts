import {
  audienceFilter,
  canTransition,
  createBody,
  DEFAULT_HOURS,
  draftToHours,
  formatEta,
  hoursError,
  hoursToDraft,
  leadCounts,
  settledLeads,
  stepPatch,
  summarizeHours,
  wizardDefaults,
  wizardSchema,
  type WizardInput,
} from "@/features/campaigns/campaigns-model";

const valid: WizardInput = {
  ...wizardDefaults("Asia/Karachi"),
  name: "October re-engagement",
  agent_id: "a1",
  tag: "Q4",
};

describe("wizardSchema", () => {
  it("accepts a complete campaign and coerces numbers", () => {
    const r = wizardSchema.safeParse({ ...valid, max_concurrency: "5", calls_per_minute: 12, max_calls: "" });
    expect(r.success).toBe(true);
    if (!r.success) return;
    expect(r.data.max_concurrency).toBe(5);
    expect(r.data.calls_per_minute).toBe(12);
    expect(r.data.max_calls).toBeNull();
    expect(r.data.tag).toBe("q4");
  });

  const issue = (input: WizardInput) => {
    const r = wizardSchema.safeParse(input);
    return r.success ? null : r.error.issues.map((i) => `${i.path.join(".")}: ${i.message}`);
  };

  it("requires a name and an agent", () => {
    expect(issue({ ...valid, name: "  " })).toContain("name: Name the campaign");
    expect(issue({ ...valid, agent_id: "" })).toContain("agent_id: Choose an agent");
  });

  it("requires an explicit audience so nobody dials everyone by accident", () => {
    expect(issue({ ...valid, tag: "" })).toContain("stage_id: Choose at least one filter so the audience is explicit.");
    expect(issue({ ...valid, tag: "", owner: "unassigned" })).toBeNull();
    expect(issue({ ...valid, tag: "bad tag!" })).toContain("tag: Tags use letters, numbers, - _ :");
  });

  it("checks the schedule window and the calling hours", () => {
    expect(issue({ ...valid, start_at: "2026-10-20T09:00:00Z", end_at: "2026-10-19T09:00:00Z" })).toContain("end_at: The end must be after the start.");
    const noDays = { ...valid, hours: Object.fromEntries(Object.entries(valid.hours).map(([d, w]) => [d, { ...w, enabled: false }])) as WizardInput["hours"] };
    expect(issue(noDays)).toContain("hours: Pick at least one day on which calls may be placed.");
    const inverted = { ...valid, hours: { ...valid.hours, mon: { enabled: true, from: "18:00", to: "09:00" } } };
    expect(issue(inverted)).toContain("hours: Monday: the window must start before it ends.");
  });

  it("bounds pacing and retries like the API", () => {
    expect(issue({ ...valid, max_concurrency: "0" })).toContain("max_concurrency: Concurrency must be at least 1");
    expect(issue({ ...valid, max_concurrency: "51" })).toContain("max_concurrency: Concurrency must be at most 50");
    expect(issue({ ...valid, calls_per_minute: "x" })).toContain("calls_per_minute: Calls per minute must be a whole number");
    expect(issue({ ...valid, max_attempts: "6" })).toContain("max_attempts: Attempts must be at most 5");
    expect(issue({ ...valid, backoff_minutes: "0" })).toContain("backoff_minutes: Backoff must be at least 1");
    expect(issue({ ...valid, max_calls: "0" })).toContain("max_calls: Max calls must be at least 1");
  });
});

describe("request bodies", () => {
  const out = wizardSchema.parse(valid);

  it("builds the create body as a dry run with the audience filter", () => {
    const body = createBody(out);
    expect(body.mode).toBe("dry_run");
    expect(body.lead_filter).toEqual({ stage_id: null, owner_user_id: null, unassigned: false, tag: "q4", source: null, qualification_status: null });
    expect(body.calling_hours).toMatchObject({ mon: [["09:00", "18:00"]], sat: [], sun: [] });
    expect(body.retry_policy).toEqual({ max_attempts: 2, retry_on: ["no_answer", "busy"], backoff_minutes: 30 });
    expect(body.timezone).toBe("Asia/Karachi");
  });

  it("maps the owner choice to owner_user_id or unassigned", () => {
    expect(audienceFilter({ ...out, owner: "unassigned" })).toMatchObject({ owner_user_id: null, unassigned: true });
    expect(audienceFilter({ ...out, owner: "u1" })).toMatchObject({ owner_user_id: "u1", unassigned: false });
  });

  it("leaves frozen fields out of the patch once the campaign has started", () => {
    expect(stepPatch(0, out, false)).toEqual({ name: out.name, objective: null, agent_id: "a1" });
    expect(stepPatch(0, out, true)).toEqual({ name: out.name, objective: null });
    expect(stepPatch(1, out, true)).toEqual({});
    expect(stepPatch(1, out, false)).toHaveProperty("lead_filter");
    expect(stepPatch(3, out, true)).toMatchObject({ max_concurrency: 2, calls_per_minute: 10, max_calls: null });
  });
});

describe("calling hours", () => {
  it("round-trips through the editor model and keeps extra windows", () => {
    const api = { mon: [["08:00", "12:00"], ["13:00", "17:00"]] as [string, string][], wed: [["10:00", "24:00"]] as [string, string][] };
    const draft = hoursToDraft(api);
    expect(draft.mon).toEqual({ enabled: true, from: "08:00", to: "12:00" });
    expect(draft.tue).toEqual({ ...DEFAULT_HOURS.tue, enabled: false });
    expect(draft.wed).toEqual({ enabled: true, from: "10:00", to: "24:00" });
    expect(draftToHours({ ...draft, mon: { ...draft.mon, to: "11:30" } }, api)).toMatchObject({ mon: [["08:00", "11:30"], ["13:00", "17:00"]], tue: [], wed: [["10:00", "24:00"]] });
  });

  it("validates and summarises", () => {
    expect(hoursError(DEFAULT_HOURS)).toBeNull();
    expect(summarizeHours(draftToHours(DEFAULT_HOURS))).toBe("Mon–Fri 09:00–18:00");
    expect(summarizeHours({ mon: [["09:00", "12:00"]], tue: [["09:00", "12:00"]], sat: [["10:00", "14:00"]] })).toBe("Mon, Tue 09:00–12:00 · Sat 10:00–14:00");
    expect(summarizeHours({})).toBe("No calling hours");
  });
});

describe("lifecycle and progress", () => {
  it("mirrors the API's allowed transitions", () => {
    expect(canTransition("draft", "start")).toBe(true);
    expect(canTransition("running", "start")).toBe(false);
    expect(canTransition("running", "pause")).toBe(true);
    expect(canTransition("scheduled", "pause")).toBe(true);
    expect(canTransition("paused", "resume")).toBe(true);
    expect(canTransition("paused", "pause")).toBe(false);
    expect(canTransition("completed", "cancel")).toBe(false);
    expect(canTransition("draft", "cancel")).toBe(true);
  });

  it("reads lead counts from progress, falling back to cached stats, and counts settled leads", () => {
    const fromStats = leadCounts(undefined, { stats: { leads: { pending: 3, completed: 2 } } } as never);
    expect(fromStats).toEqual({ completed: 2, calling: 0, pending: 3, skipped: 0, exhausted: 0, failed: 0 });
    expect(settledLeads(fromStats)).toBe(2);
    const fromProgress = leadCounts({ leads: { calling: 1, skipped: 4 } } as never);
    expect(settledLeads(fromProgress)).toBe(4);
  });

  it("formats an ETA", () => {
    expect(formatEta(null)).toBeNull();
    expect(formatEta(30)).toBe("under a minute");
    expect(formatEta(720)).toBe("about 12 min");
    expect(formatEta(5400)).toBe("about 1.5 h");
    expect(formatEta(50_000)).toBe("about 14 h");
  });
});
