import { agentFormSchema, fromForm, toForm, variableNameProblem, type AgentForm } from "@/features/voice/agent-form";
import { availableVariables } from "@/features/voice/agent-variables";
import type { AgentConfig, Connection } from "@/features/voice/queries";
import { AUDIO_LIMITS, audioFileProblem } from "@/features/voice/settings/agent-settings-api";
import { classifierConnections, normalizeDomain } from "@/features/voice/settings/settings-model";

const base = {
  company_name: "Acme",
  greeting: "Hi {{first_name}}",
  instructions: "Qualify the lead politely and briefly.",
  closing: "Thank them and say goodbye.",
  end_condition: "The person is done talking.",
} as AgentConfig;
const AUDIO = "6f1c2f8e-6f4b-4a43-9a5e-2c1d3b4a5f60";

function issues(form: AgentForm): Record<string, string> {
  const parsed = agentFormSchema.safeParse(form);
  if (parsed.success) return {};
  return Object.fromEntries(parsed.error.issues.map((i) => [i.path.join("."), i.message]));
}

describe("agent form: new call settings", () => {
  it("loads configs saved before these settings with safe defaults", () => {
    const f = toForm(base);
    expect(f.call_settings.ambient).toEqual({ enabled: false, volume: 0.3, audio_id: null });
    expect(f.call_settings.voicemail).toMatchObject({
      enabled: true,
      action: "hangup",
      message_format: "text",
      message_audio_id: null,
      screening_format: "text",
      screening_message: "",
      screening_wait_seconds: 30,
      classifier: { model: "agent", connection_id: "", model_name: "", instructions: "" },
    });
    expect(f.variables).toEqual([]);
    expect(issues(f)).toEqual({});
  });

  it("round-trips background noise, voicemail and screening, the classifier and variables", () => {
    const config: AgentConfig = {
      ...base,
      variables: [{ name: "office_name", value: "Gulberg office" }],
      call_settings: {
        ambient: { enabled: true, volume: 0.45, audio_id: AUDIO },
        voicemail: {
          enabled: true,
          action: "leave_message",
          message_format: "recording",
          message: "  Please call us back.  ",
          message_audio_id: AUDIO,
          screening_format: "text",
          screening_message: "This is Sara from Acme.",
          screening_audio_id: null,
          screening_wait_seconds: 12,
          classifier: { model: "connection", connection_id: "conn-1", model_name: "gpt-4.1-mini", instructions: "Callers greet in Urdu." },
        },
      },
    };
    const f = toForm(config);
    expect(issues(f)).toEqual({});
    const out = fromForm(f);
    expect(out.variables).toEqual([{ name: "office_name", value: "Gulberg office" }]);
    expect(out.call_settings?.ambient).toEqual({ enabled: true, volume: 0.45, audio_id: AUDIO });
    expect(out.call_settings?.voicemail).toEqual({
      enabled: true,
      action: "leave_message",
      message_format: "recording",
      message: "Please call us back.",
      message_audio_id: AUDIO,
      screening_format: "text",
      screening_message: "This is Sara from Acme.",
      screening_audio_id: null,
      screening_wait_seconds: 12,
      classifier: { model: "connection", connection_id: "conn-1", model_name: "gpt-4.1-mini", instructions: "Callers greet in Urdu." },
    });
  });

  it("drops the separate model's fields and blank texts when they don't apply", () => {
    const f = toForm(base);
    f.call_settings.voicemail.classifier = { model: "agent", connection_id: "conn-1", model_name: "gpt", instructions: "   " };
    const vm = fromForm(f).call_settings?.voicemail;
    expect(vm?.classifier).toEqual({ model: "agent", connection_id: null, model_name: null, instructions: null });
    expect(vm?.message).toBeNull();
    expect(vm?.screening_message).toBeNull();
  });

  it("validates voicemail messages, recordings and the separate model", () => {
    const f = toForm(base);
    f.call_settings.voicemail.action = "leave_message";
    expect(issues(f)["call_settings.voicemail.message"]).toBe("Write the message to leave");
    f.call_settings.voicemail.message_format = "recording";
    expect(issues(f)["call_settings.voicemail.message_audio_id"]).toBe("Upload the recording to leave");
    f.call_settings.voicemail.message_audio_id = AUDIO;
    f.call_settings.voicemail.screening_format = "recording";
    expect(issues(f)["call_settings.voicemail.screening_audio_id"]).toMatch(/call screeners/);
    f.call_settings.voicemail.screening_format = "text";
    f.call_settings.voicemail.classifier.model = "connection";
    const e = issues(f);
    expect(e["call_settings.voicemail.classifier.connection_id"]).toBe("Choose a connection");
    expect(e["call_settings.voicemail.classifier.model_name"]).toBe("Enter the model");
    f.call_settings.voicemail.screening_wait_seconds = 61;
    expect(issues(f)["call_settings.voicemail.screening_wait_seconds"]).toBe("At most 60 seconds");
    f.call_settings.voicemail.enabled = false;
    expect(Object.keys(issues(f)).filter((k) => k.startsWith("call_settings.voicemail.classifier"))).toEqual([]);
  });

  it("validates variable names and values the way the API does", () => {
    expect(variableNameProblem("office_name")).toBeNull();
    expect(variableNameProblem("first_name")).toMatch(/Filled from the lead/);
    expect(variableNameProblem("phone_number")).toMatch(/Reserved/);
    expect(variableNameProblem("atlas_x")).toMatch(/Reserved/);
    expect(variableNameProblem("Office")).toMatch(/Lowercase/);
    expect(variableNameProblem("a".repeat(41))).toMatch(/max 40/);
    const f = toForm(base);
    f.variables = [
      { name: "promo", value: "SPRING10" },
      { name: "promo", value: "again" },
      { name: "company", value: "x" },
      { name: "note", value: "{{first_name}}" },
      { name: "empty", value: "  " },
    ];
    const e = issues(f);
    expect(e["variables.1.name"]).toBe("Duplicate name");
    expect(e["variables.2.name"]).toMatch(/Filled from the lead/);
    expect(e["variables.3.value"]).toBe("Values cannot contain { or }");
    expect(e["variables.4.value"]).toBe("Enter a value");
    expect(e["variables.0.name"]).toBeUndefined();
  });

  it("offers lead fields first, then the agent's valid variables", () => {
    const vars = availableVariables([
      { name: "office_name", value: "Gulberg" },
      { name: "email", value: "dup of a lead field" },
      { name: "Bad Name", value: "x" },
      { name: "office_name", value: "again" },
    ]);
    expect(vars.slice(0, 6).every((v) => v.source === "lead")).toBe(true);
    expect(vars.slice(6)).toEqual([{ name: "office_name", source: "agent", value: "Gulberg" }]);
  });
});

describe("settings helpers", () => {
  it("normalises website domains like the API", () => {
    expect(normalizeDomain("https://WWW.Example.com/pricing?x=1")).toBe("www.example.com");
    expect(normalizeDomain("*.example.com")).toBe("*.example.com");
    expect(normalizeDomain("localhost:3000")).toBe("localhost:3000");
    for (const bad of ["*", "*.com", "exa mple.com", "example.com:70000", ""]) expect(normalizeDomain(bad)).toBeNull();
  });

  it("offers only connections the voice service can run voicemail detection on", () => {
    const conns = [
      { id: "a", provider: "openai", connection_settings: {}, hosted_trial: false },
      { id: "b", provider: "openai", connection_settings: { base_url: "http://proxy" }, hosted_trial: false },
      { id: "c", provider: "speaches", connection_settings: { base_url: "http://local" }, hosted_trial: false },
      { id: "d", provider: "groq", connection_settings: {}, hosted_trial: false },
      { id: "e", provider: "hosted_trial", connection_settings: {}, hosted_trial: true },
    ] as unknown as Connection[];
    expect(classifierConnections(conns).map((c) => c.id)).toEqual(["a", "d"]);
  });

  it("checks sound files before uploading", () => {
    expect(audioFileProblem(new File(["x"], "office.wav"), "ambient")).toBeNull();
    expect(audioFileProblem(new File(["x"], "vm.MP3"), "message")).toBeNull();
    expect(audioFileProblem(new File(["x"], "clip.ogg"), "ambient")).toBe("Choose a WAV or MP3 file.");
    expect(audioFileProblem(new File([], "empty.wav"), "ambient")).toBe("That file is empty.");
    const big = new File([new Uint8Array(AUDIO_LIMITS.message + 1)], "vm.wav");
    expect(audioFileProblem(big, "message")).toMatch(/limit is 5 MB/);
    expect(audioFileProblem(big, "ambient")).toBeNull();
  });
});
