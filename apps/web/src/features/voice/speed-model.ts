/** Pure helpers for the AI providers "Speed" section. */
import type { LatencyPreset, PresetChoice } from "@/features/calls/latency-types";
import type { Catalog, Connection } from "@/features/voice/queries";

/** One server type serves every self-hosted role, so its catalog title (per service) would mislabel the others. */
const SELF_HOSTED = new Set(["speaches"]);

/** Provider id → its catalog title, from any service; falls back to the id. */
export function providerTitle(id: string, catalog: Catalog | undefined): string {
  if (SELF_HOSTED.has(id)) return "Self-hosted models";
  if (!catalog) return id;
  for (const kind of ["stt", "llm", "tts", "embeddings"] as const) {
    const hit = catalog[kind].find((p) => p.id === id);
    if (hit) return hit.title;
  }
  return id;
}

/** Providers a preset needs that have no connection yet. */
export function missingProviders(preset: LatencyPreset, connections: Connection[] | undefined): string[] {
  const have = new Set((connections ?? []).map((c) => c.provider));
  return preset.requires.filter((p) => !have.has(p));
}

/** A 409 from apply names the missing providers, either bare or inside the error envelope. */
export function missingFromError(body: unknown): string[] | null {
  const b = (body ?? {}) as { missing?: unknown; error?: { details?: { missing?: unknown } | unknown }; detail?: { missing?: unknown } };
  const raw = b.missing ?? (b.error?.details as { missing?: unknown } | undefined)?.missing ?? b.detail?.missing;
  return Array.isArray(raw) ? raw.filter((x): x is string => typeof x === "string") : null;
}

/** "Deepgram · nova-3": the provider's title plus the model or voice the preset picks, when it names one. */
export function choiceLabel(choice: PresetChoice, catalog: Catalog | undefined): string {
  const s = choice.settings ?? {};
  const detail = [s.model, s.voice, s.voice_id].find((v): v is string => typeof v === "string" && v.length > 0);
  // Self-hosted: the model is the meaningful part ("qwen2.5:1.5b"), not the server type.
  if (SELF_HOSTED.has(choice.provider)) return detail ? `Self-hosted · ${detail}` : "Self-hosted models";
  const title = providerTitle(choice.provider, catalog);
  return detail ? `${title} · ${detail}` : title;
}
