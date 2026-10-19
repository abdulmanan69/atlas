/** Pocket TTS voices and voice cloning: queries, mutations and helpers. */
import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import type { Connection } from "@/features/voice/queries";
import { api, ApiError, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type PocketVoice = Schemas["PocketVoice"];
export type PocketVoiceList = Schemas["PocketVoiceList"];

/** Same rule as the API: a self-hosted connection whose name or address names the server. */
const POCKET_HINTS = ["local-tts", "pocket"];
export const CLONING_TERMS_URL = "https://huggingface.co/kyutai/pocket-tts";
export const CONSENT_ATTESTATION = "I have the speaker's permission to clone their voice.";

export function isPocketConnection(c: Connection | undefined): boolean {
  if (!c || c.provider !== "speaches" || c.hosted_trial) return false;
  const haystack = [c.name, ...Object.values(c.connection_settings ?? {}).map(String)].join(" ").toLowerCase();
  return POCKET_HINTS.some((h) => haystack.includes(h));
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
