/**
 * Pocket TTS and expressive voices, voice cloning and voice design: queries, mutations and
 * helpers. The expressive voice server (Chatterbox-Turbo) has the same voice and cloning API,
 * and also says which sounds it makes ("[laugh]"), see `useSoundTags`. The voice design
 * server has it too and designs voices from a description; what a server can do is asked
 * with `useVoiceCapabilities`.
 */
import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import type { Connection } from "@/features/voice/queries";
import { MAX_NAME_CHARS } from "@/features/voice/voice-sample";
import { api, ApiError, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type PocketVoice = Schemas["PocketVoice"];
export type PocketVoiceList = Schemas["PocketVoiceList"];
export type SoundTags = Schemas["SoundTags"];
export type SampleCheck = Schemas["SampleCheck"];
export type QualityFinding = Schemas["QualityFinding"];
export type VoiceCapabilities = Schemas["VoiceCapabilities"];
export type VoiceDesignCreate = Schemas["VoiceDesignCreate"];

/**
 * Same rule as the API: a self-hosted connection whose name or address names the server
 * (/tts/v1, /voice/v1 and /design/v1 are their paths behind an AI server). An expressive
 * server with another name is still known by its answer (`useSoundTags`).
 */
const POCKET_HINTS = ["local-tts", "pocket", "/tts/v1"];
const EXPRESSIVE_HINTS = ["expressive", "chatterbox", "/voice/v1"];
const DESIGN_HINTS = ["voice-design", "voice design", "qwen", "/design/v1"];
export const CLONING_TERMS_URL = "https://huggingface.co/kyutai/pocket-tts";
export const CONSENT_ATTESTATION = "I have the speaker's permission to clone their voice.";

function haystack(c: Connection): string {
  return [c.name, ...Object.values(c.connection_settings ?? {}).map(String)].join(" ").toLowerCase();
}

/** A self-hosted voice: the kind of connection that may be expressive. */
export function isSelfHostedVoice(c: Connection | undefined): c is Connection {
  return !!c && c.provider === "speaches" && !c.hosted_trial;
}

/** A voice server with the Pocket TTS voice API: Pocket TTS, the expressive or the voice design server. */
export function isPocketConnection(c: Connection | undefined): boolean {
  if (!isSelfHostedVoice(c)) return false;
  const text = haystack(c);
  return [...POCKET_HINTS, ...EXPRESSIVE_HINTS, ...DESIGN_HINTS].some((h) => text.includes(h));
}

/** A voice this organization made: cloned from a recording or designed from a description. */
export function isCustomVoice(v: PocketVoice): boolean {
  return v.cloned || v.designed === true;
}

export const voiceCapabilitiesKey = (o: string | null, connectionId: string) => ["org", o, "ai", "voice-capabilities", connectionId] as const;

/**
 * What a voice connection's server can do (clone, design, instruct; its languages). Derived
 * by the API for an older server, so every Pocket-style connection has an answer. Pass null
 * to skip.
 */
export function useVoiceCapabilities(connectionId: string | null | undefined) {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: voiceCapabilitiesKey(o, connectionId ?? ""),
    queryFn: () => unwrap(api.GET("/api/v1/ai/connections/{connection_id}/capabilities", { params: { path: { connection_id: connectionId! } } })),
    staleTime: 60_000,
    enabled: !!o && can && !!connectionId,
    retry: false,
  });
}

export const soundTagsKey = (o: string | null, connectionId: string) => ["org", o, "ai", "sound-tags", connectionId] as const;

/** Whether a voice connection makes sounds such as [laugh], and which. Pass null to skip. */
export function useSoundTags(connectionId: string | null | undefined) {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: soundTagsKey(o, connectionId ?? ""),
    queryFn: () => unwrap(api.GET("/api/v1/ai/connections/{connection_id}/sound-tags", { params: { path: { connection_id: connectionId! } } })),
    staleTime: 60_000,
    enabled: !!o && can && !!connectionId,
    retry: false,
  });
}

export const pocketVoicesKey = (o: string | null, connectionId: string) => ["org", o, "ai", "pocket-voices", connectionId] as const;

export function usePocketVoices(connectionId: string) {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: pocketVoicesKey(o, connectionId),
    queryFn: () => unwrap(api.GET("/api/v1/ai/connections/{connection_id}/voices", { params: { path: { connection_id: connectionId } } })),
    staleTime: 60_000,
    enabled: !!o && can && !!connectionId,
    retry: false,
  });
}

export function cloneVoice(connectionId: string, name: string, wav: Blob): Promise<PocketVoice> {
  const fd = new FormData();
  fd.append("name", name.trim());
  fd.append("consent_attestation", "true");
  fd.append("audio", wav, "sample.wav");
  return unwrap(
    api.POST("/api/v1/ai/connections/{connection_id}/voices", {
      params: { path: { connection_id: connectionId } },
      body: { name: name.trim(), consent_attestation: true, audio: "sample.wav" },
      bodySerializer: () => fd,
    }),
  );
}

/**
 * Measures a recording as cloning would, without cloning: the findings the clone form shows
 * before the user confirms. Send the original file where there is one, so the API sees its
 * sample rate, channels and format. A `problem` finding means the clone would be refused.
 */
export function checkVoiceSample(connectionId: string, audio: Blob, filename: string): Promise<SampleCheck> {
  const fd = new FormData();
  fd.append("audio", audio, filename);
  return unwrap(
    api.POST("/api/v1/ai/connections/{connection_id}/voices/check", {
      params: { path: { connection_id: connectionId } },
      body: { audio: filename },
      bodySerializer: () => fd,
    }),
  );
}

/** The findings a refused clone (422) came with, if it was refused for the recording. */
export function refusedFindings(err: unknown): QualityFinding[] | null {
  if (!(err instanceof ApiError) || err.status !== 422) return null;
  const details = err.details as { findings?: unknown } | null;
  return Array.isArray(details?.findings) ? (details.findings as QualityFinding[]) : null;
}

export function deleteVoice(connectionId: string, voiceId: string): Promise<unknown> {
  return unwrap(api.DELETE("/api/v1/ai/connections/{connection_id}/voices/{voice_id}", { params: { path: { connection_id: connectionId, voice_id: voiceId } } }));
}

// ------------------------------------------------------------------ voice design

export const MIN_DESCRIPTION_CHARS = 10;
export const MAX_DESCRIPTION_CHARS = 300;
/** Shown as the description's placeholder: the kind of words the server works from. */
export const DESCRIPTION_EXAMPLES = [
  "A warm, unhurried woman in her forties with a soft Irish accent.",
  "A bright, upbeat young man who speaks quickly and clearly.",
  "A calm, deep-voiced older man with a slight northern English accent.",
] as const;

export type DesignDraft = { name: string; description: string };
export type DesignErrors = Partial<Record<"name" | "description", string>>;

/** Same checks as the API, so nothing is sent that it would refuse. */
export function validateDesign(draft: DesignDraft): DesignErrors {
  const errors: DesignErrors = {};
  const name = draft.name.trim();
  if (!name) errors.name = "Give the voice a name.";
  else if (name.length > MAX_NAME_CHARS) errors.name = `Names are at most ${MAX_NAME_CHARS} characters.`;
  const description = draft.description.replace(/\s+/g, " ").trim();
  if (description.length < MIN_DESCRIPTION_CHARS) errors.description = `Describe the voice in at least ${MIN_DESCRIPTION_CHARS} characters: who is speaking and how.`;
  else if (description.length > MAX_DESCRIPTION_CHARS) errors.description = `Descriptions are at most ${MAX_DESCRIPTION_CHARS} characters.`;
  return errors;
}

/** Owners and admins. The voice belongs to the organization at once; `fetchVoiceSample` plays it. */
export function designVoice(connectionId: string, draft: DesignDraft & { language?: string | null }): Promise<PocketVoice> {
  const body: VoiceDesignCreate = { name: draft.name.trim(), description: draft.description.replace(/\s+/g, " ").trim() };
  if (draft.language) body.language = draft.language;
  return unwrap(api.POST("/api/v1/ai/connections/{connection_id}/voices/design", { params: { path: { connection_id: connectionId } }, body }));
}

/** The server's short preview of one of this organization's voices, as WAV. */
export function fetchVoiceSample(connectionId: string, voiceId: string): Promise<Blob> {
  return unwrap(
    api.GET("/api/v1/ai/connections/{connection_id}/voices/{voice_id}/sample", {
      params: { path: { connection_id: connectionId, voice_id: voiceId } },
      parseAs: "blob",
    }),
  ) as Promise<Blob>;
}

/** The Pocket TTS server has no voice-cloning weights (no token, or terms not accepted). */
export function cloningUnavailable(err: unknown): boolean {
  return err instanceof ApiError && err.status === 501;
}

export type VoiceCloningStatus = Schemas["VoiceCloningStatus"];

export const cloningStatusKey = (o: string | null, connectionId: string) => ["org", o, "ai", "voice-cloning", connectionId] as const;

/** Whether voice cloning is on for the connection's server; polls while it is being turned on. */
export function useCloningStatus(connectionId: string) {
  const o = useOrgId();
  return useQuery({
    queryKey: cloningStatusKey(o, connectionId),
    queryFn: () => unwrap(api.GET("/api/v1/ai/connections/{connection_id}/voice-cloning", { params: { path: { connection_id: connectionId } } })),
    enabled: !!o && !!connectionId,
    retry: false,
    refetchInterval: (q) => (q.state.data?.state === "enabling" ? 2000 : false),
  });
}

/** Owners only. The token goes to the voice server; ATLAS never stores it. */
export function enableCloning(connectionId: string, hfToken: string): Promise<VoiceCloningStatus> {
  return unwrap(
    api.PUT("/api/v1/ai/connections/{connection_id}/voice-cloning", {
      params: { path: { connection_id: connectionId } },
      body: { hf_token: hfToken.trim(), terms_accepted: true },
    }),
  );
}

export function disableCloning(connectionId: string): Promise<unknown> {
  return unwrap(api.DELETE("/api/v1/ai/connections/{connection_id}/voice-cloning", { params: { path: { connection_id: connectionId } } }));
}
