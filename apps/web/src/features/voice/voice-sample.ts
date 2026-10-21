/**
 * Voice samples for Pocket TTS voice cloning: client-side checks, and conversion of any
 * sample (WAV, MP3, M4A, OGG, WebM, FLAC or a browser recording) to a small 24 kHz mono 16-bit WAV, the only
 * format the Pocket TTS server reads. The server re-checks everything.
 */

export const MIN_SAMPLE_SECONDS = 5;
export const MAX_SAMPLE_SECONDS = 30;
export const MAX_SAMPLE_BYTES = 10 * 1024 * 1024;
export const MAX_NAME_CHARS = 60;
export const TARGET_RATE = 24000;

export const SAMPLE_TYPES: { ext: string; mime: string[] }[] = [
  { ext: ".wav", mime: ["audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave"] },
  { ext: ".mp3", mime: ["audio/mpeg", "audio/mp3"] },
  { ext: ".m4a", mime: ["audio/mp4", "audio/x-m4a", "audio/m4a", "audio/aac"] },
  { ext: ".ogg", mime: ["audio/ogg", "audio/opus", "audio/vorbis"] },
  { ext: ".webm", mime: ["audio/webm"] },
  { ext: ".flac", mime: ["audio/flac", "audio/x-flac"] },
];
export const SAMPLE_ACCEPT = SAMPLE_TYPES.flatMap((t) => [t.ext, ...t.mime]).join(",");

/** A sample ready to upload, with what the form needs to show and check. */
export interface VoiceSample {
  wav: Blob;
  seconds: number;
  source: "upload" | "recording";
  label: string;
}

export interface CloneDraft {
  name: string;
  consent: boolean;
  sample: VoiceSample | null;
}

export type CloneErrors = Partial<Record<"name" | "sample" | "consent", string>>;

export function formatSeconds(s: number): string {
  const whole = Math.max(0, Math.floor(s));
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

/** Type and size of a chosen file, before it is decoded. */
export function validateSampleFile(file: File): string | null {
  const lower = file.name.toLowerCase();
  const known = SAMPLE_TYPES.some((t) => lower.endsWith(t.ext) || t.mime.includes(file.type));
  if (!known) return `“${file.name}” isn't a supported type. Use WAV, MP3, M4A, OGG, WebM or FLAC.`;
  if (file.size === 0) return `“${file.name}” is empty.`;
  if (file.size > MAX_SAMPLE_BYTES) return `“${file.name}” is larger than 10 MB.`;
  return null;
}

export function validateDuration(seconds: number): string | null {
  if (!Number.isFinite(seconds) || seconds < MIN_SAMPLE_SECONDS) {
    return `The sample is ${seconds.toFixed(1)} s long. Use at least ${MIN_SAMPLE_SECONDS} seconds of speech.`;
  }
  if (seconds > MAX_SAMPLE_SECONDS) {
    return `The sample is ${seconds.toFixed(1)} s long. Use at most ${MAX_SAMPLE_SECONDS} seconds: trim it, or record a new one.`;
  }
  return null;
}

export function validateClone(draft: CloneDraft): CloneErrors {
  const errors: CloneErrors = {};
  const name = draft.name.trim();
  if (!name) errors.name = "Give the voice a name.";
  else if (name.length > MAX_NAME_CHARS) errors.name = `Names are at most ${MAX_NAME_CHARS} characters.`;
  if (!draft.sample) errors.sample = "Add a voice sample: upload a file or record one.";
  else {
    const problem = validateDuration(draft.sample.seconds) ?? (draft.sample.wav.size > MAX_SAMPLE_BYTES ? "The sample is larger than 10 MB." : null);
    if (problem) errors.sample = problem;
  }
  if (!draft.consent) errors.consent = "Confirm that the speaker gave permission before cloning their voice.";
  return errors;
}

/** 16-bit PCM mono WAV from samples in [-1, 1]. */
export function encodeWav(samples: Float32Array, rate: number): Blob {
  const buffer = new ArrayBuffer(44 + samples.length * 2);
  const view = new DataView(buffer);
  const text = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) view.setUint8(offset + i, s.charCodeAt(i));
  };
  text(0, "RIFF");
  view.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true); // PCM
  view.setUint16(22, 1, true); // mono
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  text(36, "data");
  view.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const v = Math.max(-1, Math.min(1, samples[i] ?? 0));
    view.setInt16(44 + i * 2, v < 0 ? v * 0x8000 : v * 0x7fff, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

type AudioContextCtor = typeof AudioContext;

/**
 * Decode any format the browser can play, then downmix and resample to 24 kHz mono WAV.
 * Throws a readable Error when the browser cannot decode the file.
 */
export async function toVoiceSample(blob: Blob, source: VoiceSample["source"], label: string): Promise<VoiceSample> {
  const Ctx: AudioContextCtor | undefined = window.AudioContext ?? (window as unknown as { webkitAudioContext?: AudioContextCtor }).webkitAudioContext;
  if (!Ctx || typeof OfflineAudioContext === "undefined") throw new Error("This browser can't process audio. Use a current Chrome, Edge, Firefox or Safari.");
  const ctx = new Ctx();
  let decoded: AudioBuffer;
  try {
    decoded = await ctx.decodeAudioData(await blob.arrayBuffer());
  } catch {
    throw new Error("This file couldn't be read as audio by this browser. Use a WAV, MP3 or M4A file.");
  } finally {
    void ctx.close();
  }
  const seconds = decoded.duration;
  // Too long to be accepted: skip resampling; the form shows the duration error.
  if (seconds > MAX_SAMPLE_SECONDS) return { wav: new Blob(), seconds, source, label };
  const offline = new OfflineAudioContext(1, Math.max(1, Math.ceil(seconds * TARGET_RATE)), TARGET_RATE);
  const node = offline.createBufferSource();
  node.buffer = decoded;
  node.connect(offline.destination);
  node.start();
  const rendered = await offline.startRendering();
  return { wav: encodeWav(rendered.getChannelData(0), TARGET_RATE), seconds, source, label };
}
