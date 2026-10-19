/**
 * A sound file a setting points at: upload (WAV/MP3, checked here and by the API), preview,
 * replace, and go back to the default. The file is kept by ATLAS and reaches the voice
 * service when the agent is published.
 */
import { useMutation } from "@tanstack/react-query";
import { FileAudio, Loader2, Pause, Play, RotateCcw, Upload } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { useInvalidate } from "@/features/queries";
import { AUDIO_ACCEPT, audioFileProblem, fetchAudioBlob, formatSize, uploadAudio, useAgentAudio, type AudioPurpose } from "@/features/voice/settings/agent-settings-api";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

/** Plays one file at a time; object URLs are released when the field goes away. */
function usePreview(agentId: string) {
  const audio = useRef<HTMLAudioElement | null>(null);
  const urls = useRef(new Map<string, string>());
  const [playing, setPlaying] = useState<string | null>(null);
  const [loading, setLoading] = useState<string | null>(null);

  useEffect(() => {
    const cache = urls.current;
    return () => {
      audio.current?.pause();
      for (const url of cache.values()) URL.revokeObjectURL(url);
      cache.clear();
    };
  }, []);

  const stop = () => {
    audio.current?.pause();
    setPlaying(null);
  };

  const toggle = async (audioId: string) => {
    if (playing === audioId) return stop();
    stop();
    try {
      let url = urls.current.get(audioId);
      if (!url) {
        setLoading(audioId);
        url = URL.createObjectURL(await fetchAudioBlob(agentId, audioId));
        urls.current.set(audioId, url);
      }
      const el = audio.current ?? new Audio();
      audio.current = el;
      el.src = url;
      el.onended = () => setPlaying(null);
      await el.play();
      setPlaying(audioId);
    } catch (e) {
      toast.error(e instanceof Error && e.name === "NotAllowedError" ? "The browser blocked playback. Click play again." : `Couldn't play the file. ${errorMessage(e)}`);
    } finally {
      setLoading(null);
    }
  };
  return { playing, loading, toggle, stop };
}

export function AudioFileField({
  agentId,
  purpose,
  value,
  onChange,
  disabled,
  emptyText,
  uploadLabel,
  resetLabel = "Remove",
  error,
  label,
}: {
  agentId: string;
  purpose: AudioPurpose;
  value: string | null;
  onChange: (audioId: string | null) => void;
  disabled?: boolean;
  /** Shown when no file is chosen. */
  emptyText: string;
  uploadLabel: string;
  resetLabel?: string;
  error?: string;
  /** Accessible name for the group, e.g. "Custom background sound". */
  label: string;
}) {
  const id = useId();
  const input = useRef<HTMLInputElement>(null);
  const invalidate = useInvalidate();
  const files = useAgentAudio(agentId);
  const [problem, setProblem] = useState<string | null>(null);
  const preview = usePreview(agentId);
  const current = value ? files.data?.find((f) => f.id === value) : undefined;

  const send = useMutation({
    mutationFn: (file: File) => uploadAudio(agentId, purpose, file),
    onSuccess: (row) => {
      void invalidate("agent", agentId, "audio");
      preview.stop();
      onChange(row.id);
      toast.success(`Uploaded ${row.filename}. Save and publish to use it on calls.`);
    },
    onError: (e) => setProblem(errorMessage(e)),
  });

  const choose = (file: File | undefined) => {
    if (input.current) input.current.value = "";
    if (!file) return;
    const bad = audioFileProblem(file, purpose);
    setProblem(bad);
    if (!bad) send.mutate(file);
  };

  const shown = problem ?? error;
  const limit = purpose === "ambient" ? "10 MB" : "5 MB";

  return (
    <div role="group" aria-label={label} aria-describedby={shown ? `${id}-error` : undefined} className="flex flex-col gap-2">
      <input
        ref={input}
        id={`${id}-file`}
        type="file"
        accept={AUDIO_ACCEPT}
        className="sr-only"
        tabIndex={-1}
        aria-hidden
        onChange={(e) => choose(e.target.files?.[0])}
      />
      {value ? (
        <div className={cn("flex items-center gap-2 rounded-md border bg-surface-2/50 py-1.5 pl-1.5 pr-2", shown ? "border-danger/50" : "border-line")}>
          <Button
            type="button"
            size="icon-sm"
            variant="ghost"
            aria-label={preview.playing === value ? "Pause preview" : "Play preview"}
            disabled={preview.loading === value}
            onClick={() => void preview.toggle(value)}
          >
            {preview.loading === value ? <Loader2 className="animate-spin" /> : preview.playing === value ? <Pause /> : <Play />}
          </Button>
          <FileAudio className="size-4 shrink-0 text-faint" aria-hidden />
          <div className="min-w-0 flex-1">
            <p className="truncate text-[12.5px] font-medium text-ink" title={current?.filename}>
              {current?.filename ?? (files.isLoading ? "Loading…" : "Uploaded file")}
            </p>
            {current ? (
              <p className="font-mono text-[11px] text-muted">
                {formatSize(current.size_bytes)}
                {current.duration_seconds != null ? ` · ${current.duration_seconds.toFixed(1)} s` : ""}
              </p>
            ) : null}
          </div>
          <Button type="button" size="sm" variant="ghost" disabled={disabled || send.isPending} onClick={() => input.current?.click()}>
            {send.isPending ? <Loader2 className="animate-spin" /> : <Upload />} Replace
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={disabled}
            onClick={() => {
              preview.stop();
              setProblem(null);
              onChange(null);
            }}
          >
            <RotateCcw /> {resetLabel}
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" size="sm" disabled={disabled || send.isPending} onClick={() => input.current?.click()}>
            {send.isPending ? <Loader2 className="animate-spin" /> : <Upload />}
            {send.isPending ? "Uploading…" : uploadLabel}
          </Button>
          <span className="text-[12px] text-muted">
            {emptyText} WAV or MP3, up to {limit}.
          </span>
        </div>
      )}
      {shown ? (
        <p id={`${id}-error`} role="alert" className="text-[12px] text-danger">
          {shown}
        </p>
      ) : null}
    </div>
  );
}
