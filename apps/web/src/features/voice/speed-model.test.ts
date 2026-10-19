import { fromForm, RESPONSE_SPEEDS, speedFields, toForm } from "@/features/voice/agent-form";
import type { AgentConfig, Connection } from "@/features/voice/queries";
import { choiceLabel, missingFromError, missingProviders, providerTitle } from "@/features/voice/speed-model";

describe("speed presets", () => {
  it("reads the missing providers from a bare or enveloped 409", () => {
    expect(missingFromError({ missing: ["deepgram", "cartesia"] })).toEqual(["deepgram", "cartesia"]);
    expect(missingFromError({ error: { code: "conflict", message: "x", details: { missing: ["groq"] } } })).toEqual(["groq"]);
    expect(missingFromError({ error: { code: "conflict", message: "x" } })).toBeNull();
    expect(missingFromError(undefined)).toBeNull();
  });

  it("lists required providers without a connection, and names them from the catalog", () => {
    const connections = [{ provider: "openai" }, { provider: "deepgram" }] as Connection[];
    expect(missingProviders({ id: "p", title: "Fast", description: "", choices: { stt: { provider: "deepgram" }, llm: { provider: "openai" }, tts: { provider: "cartesia" } }, requires: ["deepgram", "cartesia"] }, connections)).toEqual(["cartesia"]);
    const catalog = { stt: [], llm: [], tts: [{ id: "cartesia", title: "Cartesia" }], embeddings: [] } as never;
    expect(providerTitle("cartesia", catalog)).toBe("Cartesia");
    expect(providerTitle("unknown", catalog)).toBe("unknown");
    expect(choiceLabel({ provider: "cartesia", settings: { model: "sonic-2" } }, catalog)).toBe("Cartesia · sonic-2");
    expect(choiceLabel({ provider: "cartesia" }, catalog)).toBe("Cartesia");
  });
});

describe("response speed in the agent form", () => {
  const config = { company_name: "Acme", greeting: "Hi", instructions: "Qualify the lead politely and briefly." } as AgentConfig;

  it("loads old configs with the defaults: balanced, shorter memory on, speech reuse off", () => {
    const f = toForm(config);
    expect(f.call_settings.response_speed).toBe("balanced");
    expect(f.call_settings.context_compaction).toBe(true);
    expect(f.call_settings.tts_cache).toBe(false);
  });

  it("reads settings saved before presets, with explicit turn fields, as custom", () => {
    expect(toForm({ ...config, call_settings: { turn_stop: "turn_analyzer", smart_turn_stop_seconds: 3 } }).call_settings.response_speed).toBe("custom");
  });

  it("round-trips the new fields", () => {
    const f = toForm({ ...config, call_settings: { response_speed: "patient", context_compaction: false, tts_cache: true } });
    const out = fromForm(f).call_settings as Record<string, unknown>;
    expect(out).toMatchObject({ response_speed: "patient", context_compaction: false, tts_cache: true });
  });

  it("maps each preset to the turn settings of the contract; custom has none", () => {
    expect(speedFields("snappy")).toMatchObject({ turn_start: "min_words", turn_start_min_words: 1, turn_stop: "transcription", smart_turn_stop_seconds: 0.8, idle_timeout_seconds: 8 });
    expect(speedFields("balanced")).toMatchObject({ turn_start_min_words: 2, smart_turn_stop_seconds: 1.5, idle_timeout_seconds: 10 });
    expect(speedFields("patient")).toMatchObject({ turn_start: "default", turn_stop: "turn_analyzer", smart_turn_stop_seconds: 2.5, idle_timeout_seconds: 15 });
    expect(speedFields("custom")).toBeNull();
    for (const s of RESPONSE_SPEEDS) expect(s.description.length).toBeGreaterThan(20);
  });
});
