/**
 * Records a voice sample in the browser (MediaRecorder). Loaded lazily by the clone dialog,
 * so the recorder code is only fetched when someone chooses to record.
 */
import { Mic, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { formatSeconds, MAX_SAMPLE_SECONDS, MIN_SAMPLE_SECONDS } from "@/features/voice/voice-sample";
import { cn } from "@/lib/utils";

const SCRIPT =
  "Hello, thanks for calling. I'm here to help you today. Could you tell me a little about what you're looking for? " +
  "We're open Monday to Friday from nine to five, and I can book a time that suits you.";

const MIME_TYPES = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4", "audio/ogg;codecs=opus", "audio/ogg"];

function pickMimeType(): string | undefined {
  if (typeof MediaRecorder === "undefined" || typeof MediaRecorder.isTypeSupported !== "function") return undefined;
  return MIME_TYPES.find((t) => MediaRecorder.isTypeSupported(t));
}

type State = "idle" | "starting" | "recording";

export default function VoiceRecorder({ onRecorded, disabled }: { onRecorded: (blob: Blob, seconds: number) => void; disabled?: boolean }) {
  const [state, setState] = useState<State>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const recorder = useRef<MediaRecorder | null>(null);
  const stream = useRef<MediaStream | null>(null);
  const timer = useRef<number | null>(null);
  const startedAt = useRef(0);
  const supported = typeof window !== "undefined" && typeof MediaRecorder !== "undefined" && !!navigator.mediaDevices?.getUserMedia;

  const cleanup = () => {
    if (timer.current !== null) window.clearInterval(timer.current);
    timer.current = null;
    stream.current?.getTracks().forEach((t) => t.stop());
    stream.current = null;
  };
  useEffect(
    () => () => {
      if (recorder.current && recorder.current.state !== "inactive") {
        recorder.current.onstop = null;
        recorder.current.stop();
      }
      cleanup();
    },
    [],
  );

  const stop = () => {
    if (recorder.current && recorder.current.state !== "inactive") recorder.current.stop();
  };

  const start = async () => {
    setError(null);
    setState("starting");
    try {
      const media = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      stream.current = media;
      const mimeType = pickMimeType();
      const rec = new MediaRecorder(media, mimeType ? { mimeType } : undefined);
      const chunks: Blob[] = [];
      rec.ondataavailable = (e) => {
        if (e.data.size > 0) chunks.push(e.data);
      };
      rec.onstop = () => {
        const seconds = (performance.now() - startedAt.current) / 1000;
        cleanup();
        setState("idle");
        onRecorded(new Blob(chunks, { type: rec.mimeType || mimeType || "audio/webm" }), seconds);
      };
      recorder.current = rec;
      startedAt.current = performance.now();
      setElapsed(0);
      rec.start(250);
      setState("recording");
      timer.current = window.setInterval(() => {
        const s = (performance.now() - startedAt.current) / 1000;
        setElapsed(s);
        if (s >= MAX_SAMPLE_SECONDS) stop();
      }, 200);
    } catch (e) {
      cleanup();
      setState("idle");
      const denied = e instanceof DOMException && (e.name === "NotAllowedError" || e.name === "SecurityError");
      setError(denied ? "Microphone access was blocked. Allow it in the browser's address bar, or upload a file instead." : "The microphone couldn't be started. Check that one is connected, or upload a file instead.");
    }
  };

  if (!supported) {
    return <p className="rounded-md bg-surface-2 px-3 py-2 text-[12.5px] text-muted">This browser can't record audio. Upload a WAV, MP3 or M4A file instead.</p>;
  }

  const recording = state === "recording";
  const pct = Math.min(100, (elapsed / MAX_SAMPLE_SECONDS) * 100);
  const enough = elapsed >= MIN_SAMPLE_SECONDS;
  return (
    <div className="flex flex-col gap-3 rounded-lg border border-line bg-surface-2/50 p-3">
      <p className="text-[12.5px] text-ink-2">
        Ask the speaker to read naturally for 10 to 20 seconds in a quiet room, for example:
        <span className="mt-1 block rounded-md bg-surface px-2.5 py-2 text-[12.5px] italic text-ink-2">“{SCRIPT}”</span>
      </p>
      <div className="flex items-center gap-3">
        {recording ? (
          <Button type="button" variant="danger" onClick={stop} aria-label="Stop recording">
            <Square /> Stop
          </Button>
        ) : (
          <Button type="button" variant="primary" onClick={() => void start()} loading={state === "starting"} disabled={disabled}>
            <Mic /> {elapsed > 0 ? "Record again" : "Start recording"}
          </Button>
        )}
        <div className="flex min-w-0 flex-1 flex-col gap-1">
          <div
            className="relative h-1.5 w-full overflow-hidden rounded-full bg-surface"
            role="progressbar"
            aria-label="Recording length"
            aria-valuemin={0}
            aria-valuemax={MAX_SAMPLE_SECONDS}
            aria-valuenow={Math.round(elapsed)}
          >
            <div className={cn("h-full rounded-full transition-[width]", enough ? "bg-success" : "bg-accent")} style={{ width: `${pct}%` }} />
            <span className="absolute inset-y-0 w-px bg-ink/30" style={{ left: `${(MIN_SAMPLE_SECONDS / MAX_SAMPLE_SECONDS) * 100}%` }} aria-hidden />
          </div>
          <span className="font-mono text-[11.5px] text-muted" aria-live="polite">
            {recording ? <span className="mr-1.5 inline-block size-2 animate-pulse rounded-full bg-danger align-middle" aria-hidden /> : null}
            {formatSeconds(elapsed)} / {formatSeconds(MAX_SAMPLE_SECONDS)}
            {recording && !enough ? ` · keep going, at least ${MIN_SAMPLE_SECONDS} s` : ""}
          </span>
        </div>
      </div>
      {error ? (
        <p role="alert" className="text-[12px] text-danger">
          {error}
        </p>
      ) : null}
    </div>
  );
}
