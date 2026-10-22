/**
 * Pocket TTS and expressive voices, and voice cloning: queries, mutations and helpers. The
 * expressive voice server (Chatterbox-Turbo) has the same voice and cloning API, and also
 * says which sounds it makes ("[laugh]"), see `useSoundTags`.
 */
import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import type { Connection } from "@/features/voice/queries";
import { api, ApiError, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type PocketVoice = Schemas["PocketVoice"];
export type PocketVoiceList = Schemas["PocketVoiceList"];
export type SoundTags = Schemas["SoundTags"];

/**
 * Same rule as the API: a self-hosted connection whose name or address names the server
 * (/tts/v1 and /voice/v1 are their paths behind an AI server). An expressive server with
 * another name is still known by its answer (`useSoundTags`).
 */
const POCKET_HINTS = ["local-tts", "pocket", "/tts/v1"];
const EXPRESSIVE_HINTS = ["expressive", "chatterbox", "/voice/v1"];
export const CLONING_TERMS_URL = "https://huggingface.co/kyutai/pocket-tts";
export const CONSENT_ATTESTATION = "I have the speaker's permission to clone their voice.";

function haystack(c: Connection): string {
  return [c.name, ...Object.values(c.connection_settings ?? {}).map(String)].join(" ").toLowerCase();
}

/** A self-hosted voice: the kind of connection that may be expressive. */
export function isSelfHostedVoice(c: Connection | undefined): c is Connection {
  return !!c && c.provider === "speaches" && !c.hosted_trial;
}

/** A voice server with the Pocket TTS voice API: Pocket TTS itself or the expressive server. */
export function isPocketConnection(c: Connection | undefined): boolean {
  if (!isSelfHostedVoice(c)) return false;
  const text = haystack(c);
  return [...POCKET_HINTS, ...EXPRESSIVE_HINTS].some((h) => text.includes(h));
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

export function deleteVoice(connectionId: string, voiceId: string): Promise<unknown> {
  return unwrap(api.DELETE("/api/v1/ai/connections/{connection_id}/voices/{voice_id}", { params: { path: { connection_id: connectionId, voice_id: voiceId } } }));
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
