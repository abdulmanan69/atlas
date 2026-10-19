/** Rules shared by the call-settings sections (and their tests). They mirror the API's checks. */
import type { Connection } from "@/features/voice/queries";

/** Quick picks for the background-noise volume. */
export const NOISE_LEVELS: { label: string; volume: number }[] = [
  { label: "Quiet", volume: 0.15 },
  { label: "Medium", volume: 0.3 },
  { label: "Noticeable", volume: 0.5 },
];

/** Providers the voice service can run voicemail detection on with an API key alone. */
export const CLASSIFIER_PROVIDERS = ["openai", "groq", "google", "openrouter"];

/** Language-model connections a separate voicemail-detection model can use. */
export function classifierConnections(connections: Connection[] | undefined): Connection[] {
  return (connections ?? []).filter((c) => CLASSIFIER_PROVIDERS.includes(c.provider) && !c.hosted_trial && !c.connection_settings?.base_url);
}

/** example.com, *.example.com, localhost:3000; a pasted URL is reduced to its host. Null when invalid. */
export function normalizeDomain(raw: string): string | null {
  const value = raw
    .trim()
    .toLowerCase()
    .replace(/^[a-z][a-z0-9+.-]*:\/\//, "")
    .split(/[/?#]/)[0]
    ?.replace(/\.$/, "");
  if (!value) return null;
  const wildcard = value.startsWith("*.");
  let host = wildcard ? value.slice(2) : value;
  const colon = host.lastIndexOf(":");
  if (colon !== -1) {
    const port = Number(host.slice(colon + 1));
    if (!Number.isInteger(port) || port < 1 || port > 65535) return null;
    host = host.slice(0, colon);
  }
  const labels = host.split(".");
  if (!host || host.length > 253 || !labels.every((l) => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(l))) return null;
  if (wildcard && labels.length < 2) return null;
  return value;
}
