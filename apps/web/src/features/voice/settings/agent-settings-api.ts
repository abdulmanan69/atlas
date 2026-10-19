/** Agent settings kept beside the versioned config: sound files, the voicemail-detection key, the website widget. */
import { useQuery } from "@tanstack/react-query";

import { useOrgId } from "@/features/queries";
import { api, unwrap, type Schemas } from "@/lib/api";
import { useHasRole } from "@/lib/session";

export type AgentAudio = Schemas["AgentAudioOut"];
export type AudioPurpose = AgentAudio["purpose"];
export type WebEmbed = Schemas["WebEmbedOut"];
export type ClassifierKey = Schemas["ClassifierKeyOut"];

export const ak = {
  audio: (o: string | null, agentId: string) => ["org", o, "agent", agentId, "audio"] as const,
  audioFile: (o: string | null, agentId: string, audioId: string) => ["org", o, "agent", agentId, "audio", audioId, "file"] as const,
  embed: (o: string | null, agentId: string) => ["org", o, "agent", agentId, "web-embed"] as const,
  classifierKey: (o: string | null, agentId: string) => ["org", o, "agent", agentId, "classifier-key"] as const,
};

const MB = 1024 * 1024;
/** The voice service's limits: background sounds 10 MB, recorded messages 5 MB. Enforced again by the API. */
export const AUDIO_LIMITS: Record<AudioPurpose, number> = { ambient: 10 * MB, message: 5 * MB };
export const AUDIO_ACCEPT = ".wav,.mp3,audio/wav,audio/x-wav,audio/mpeg";

/** A message for a file the API would refuse, or null. */
export function audioFileProblem(file: File, purpose: AudioPurpose): string | null {
  const ext = file.name.toLowerCase().split(".").pop();
  if (ext !== "wav" && ext !== "mp3") return "Choose a WAV or MP3 file.";
  if (file.size > AUDIO_LIMITS[purpose]) return `That file is ${(file.size / MB).toFixed(1)} MB. The limit is ${AUDIO_LIMITS[purpose] / MB} MB.`;
  if (file.size === 0) return "That file is empty.";
  return null;
}

export function uploadAudio(agentId: string, purpose: AudioPurpose, file: File): Promise<AgentAudio> {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("purpose", purpose);
  return unwrap(
    api.POST("/api/v1/agents/{agent_id}/audio", {
      params: { path: { agent_id: agentId } },
      body: { file: file.name, purpose },
      bodySerializer: () => fd,
    }),
  );
}

export function useAgentAudio(agentId: string) {
  const o = useOrgId();
  return useQuery({
    queryKey: ak.audio(o, agentId),
    queryFn: () => unwrap(api.GET("/api/v1/agents/{agent_id}/audio", { params: { path: { agent_id: agentId } } })),
    enabled: !!o && !!agentId,
    staleTime: 60_000,
  });
}

export function fetchAudioBlob(agentId: string, audioId: string): Promise<Blob> {
  return unwrap(
    api.GET("/api/v1/agents/{agent_id}/audio/{audio_id}/file", {
      params: { path: { agent_id: agentId, audio_id: audioId } },
      parseAs: "blob",
    }),
  ) as Promise<Blob>;
}

export function useWebEmbed(agentId: string) {
  const o = useOrgId();
  return useQuery({
    queryKey: ak.embed(o, agentId),
    queryFn: () => unwrap(api.GET("/api/v1/agents/{agent_id}/web-embed", { params: { path: { agent_id: agentId } } })),
    enabled: !!o && !!agentId,
  });
}

export function useClassifierKey(agentId: string, enabled: boolean) {
  const o = useOrgId();
  const can = useHasRole("manager");
  return useQuery({
    queryKey: ak.classifierKey(o, agentId),
    queryFn: () => unwrap(api.GET("/api/v1/agents/{agent_id}/voicemail-classifier-key", { params: { path: { agent_id: agentId } } })),
    enabled: !!o && !!agentId && can && enabled,
  });
}

export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < MB) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / MB).toFixed(1)} MB`;
}
