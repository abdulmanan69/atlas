/**
 * "Clone a voice" for Pocket TTS connections: name, a 5-30 s sample (upload or record),
 * and the speaker's permission. When voice cloning is not on for the server yet, the dialog
 * first shows the step that turns it on (owners), then the form.
 */
import { useMutation } from "@tanstack/react-query";
import { CloudUpload, FileAudio, Loader2, Pause, Play, ShieldCheck, X } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Checkbox, Input, Skeleton } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { useInvalidate } from "@/features/queries";
import { CONSENT_ATTESTATION, cloneVoice, cloningUnavailable, type PocketVoice } from "@/features/voice/voice-cloning";
import { VoiceCloningSetup } from "@/features/voice/voice-cloning-setup";
import {
  formatSeconds,
  MAX_NAME_CHARS,
  SAMPLE_ACCEPT,
  toVoiceSample,
  validateClone,
  validateSampleFile,
  type CloneErrors,
  type VoiceSample,
} from "@/features/voice/voice-sample";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

const VoiceRecorder = lazy(() => import("@/features/voice/voice-recorder"));

type Source = "upload" | "record";

export function CloneVoiceDialog({
  open,
  onOpenChange,
  connectionId,
  available,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  connectionId: string;
  /** False when voice cloning is not on for the server. */
  available: boolean;
  onCreated?: (voice: PocketVoice) => void;
}) {
  const invalidate = useInvalidate();
  const [mode, setMode] = useState<"setup" | "form">(available ? "form" : "setup");
  const [name, setName] = useState("");
  const [source, setSource] = useState<Source>("upload");
  const [sample, setSample] = useState<VoiceSample | null>(null);
  const [sampleError, setSampleError] = useState<string | null>(null);
  const [preparing, setPreparing] = useState(false);
  const [consent, setConsent] = useState(false);
  const [submitted, setSubmitted] = useState(false);

  const create = useMutation({
    mutationFn: () => cloneVoice(connectionId, name, sample!.wav),
    onSuccess: (voice) => {
      toast.success(`Voice “${voice.name}” is ready and selected. Save to use it for calls.`);
      void invalidate("ai", "pocket-voices", connectionId);
      onCreated?.(voice);
      onOpenChange(false);
    },
    onError: (e) => {
      // Turned off on the server meanwhile: back to the step that turns it on.
      if (cloningUnavailable(e)) setMode("setup");
    },
  });

  const unavailable = mode === "setup";
  const resetCreate = create.reset;
  const ready = useCallback(() => {
    setMode("form");
    resetCreate();
    void invalidate("ai", "pocket-voices", connectionId);
  }, [resetCreate, invalidate, connectionId]);
  const errors: CloneErrors = submitted ? validateClone({ name, consent, sample }) : {};
  const sampleProblem = sampleError ?? errors.sample;

  const loadBlob = async (blob: Blob, from: VoiceSample["source"], label: string) => {
    setPreparing(true);
    setSampleError(null);
    create.reset();
    try {
      setSample(await toVoiceSample(blob, from, label));
    } catch (e) {
      setSample(null);
      setSampleError(e instanceof Error ? e.message : "The sample couldn't be read.");
    } finally {
      setPreparing(false);
    }
  };

  const chooseFile = (file: File | null) => {
    if (!file) return;
    const problem = validateSampleFile(file);
    if (problem) {
      setSample(null);
      setSampleError(problem);
      return;
    }
    void loadBlob(file, "upload", file.name);
  };

  const submit = () => {
    setSubmitted(true);
    if (preparing || sampleError) return;
    if (Object.keys(validateClone({ name, consent, sample })).length) return;
    create.mutate();
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      width="max-w-xl"
      title={unavailable ? "Turn on voice cloning" : "Clone a voice"}
      description={unavailable ? "Voice cloning isn't on for this voice server yet. Built-in voices keep working." : "Create a voice for your agents from a short recording of a real person who agreed to it."}
      footer={
        unavailable ? (
          <Button onClick={() => onOpenChange(false)}>Close</Button>
        ) : (
          <>
            <Button onClick={() => onOpenChange(false)} disabled={create.isPending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={submit} loading={create.isPending} disabled={preparing}>
              {create.isPending ? "Cloning…" : "Clone voice"}
            </Button>
          </>
        )
      }
    >
      {unavailable ? (
        <VoiceCloningSetup connectionId={connectionId} onReady={ready} />
      ) : (
        <div className="flex flex-col gap-4">
          {create.error ? <FormError message={errorMessage(create.error)} /> : null}
          <Field label="Name" required error={errors.name} hint="Shown in voice pickers, for example “Sarah (front desk)”.">
            <Input value={name} maxLength={MAX_NAME_CHARS + 20} onChange={(e) => setName(e.target.value)} placeholder="Voice name" autoComplete="off" />
          </Field>

          <div className="flex flex-col gap-2">
            <div className="flex items-center justify-between gap-3">
              <span className="text-[13px] font-medium text-ink-2">
                Voice sample<span className="ml-0.5 text-danger" aria-hidden>*</span>
              </span>
              <Segmented<Source>
                label="Sample source"
                size="sm"
                value={source}
                onChange={setSource}
                options={[
                  { value: "upload", label: "Upload a file" },
                  { value: "record", label: "Record" },
                ]}
              />
            </div>
            {source === "upload" ? (
              <UploadArea onFile={chooseFile} disabled={preparing || create.isPending} />
            ) : (
              <Suspense fallback={<Skeleton className="h-28" />}>
                <VoiceRecorder disabled={preparing || create.isPending} onRecorded={(blob) => void loadBlob(blob, "recording", "Recording")} />
              </Suspense>
            )}
            {preparing ? (
              <p role="status" className="flex items-center gap-2 text-[12.5px] text-muted">
                <Loader2 className="size-3.5 animate-spin" aria-hidden /> Preparing the sample…
              </p>
            ) : sample ? (
              <SamplePreview
                sample={sample}
                onRemove={() => {
                  setSample(null);
                  setSampleError(null);
                }}
              />
            ) : null}
            {sampleProblem ? (
              <p role="alert" className="text-[12px] text-danger">
                {sampleProblem}
              </p>
            ) : (
              <p className="text-[12px] text-muted">5 to 30 seconds of one person speaking clearly, without music or other voices. WAV, MP3, M4A, OGG, WebM or FLAC, up to 10 MB.</p>
            )}
          </div>

          <div className={cn("flex flex-col gap-3 rounded-lg border px-3 py-3", errors.consent ? "border-danger/40 bg-danger-soft/40" : "border-line bg-surface-2/50")}>
            <div className="flex items-center gap-2 text-[13px] font-semibold">
              <ShieldCheck className="size-4 text-accent" aria-hidden /> Consent and fair use
            </div>
            <ul className="list-disc space-y-1 pl-5 text-[12.5px] text-ink-2">
              <li>Only clone the voice of someone who said yes. Ask first, and keep a record of their permission.</li>
              <li>Never use a cloned voice to trick people: no pretending to be the real person, no fraud or scam calls.</li>
              <li>Don't use it for anything unlawful or harmful.</li>
              <li>Your confirmation is saved in the audit log with your name and the time. The recording is not kept; only the computed voice is.</li>
            </ul>
            <label className="flex items-start gap-2 text-[13px] font-medium">
              <Checkbox aria-label={CONSENT_ATTESTATION} checked={consent} onCheckedChange={setConsent} className="mt-0.5" />
              <span>{CONSENT_ATTESTATION}</span>
            </label>
            {errors.consent ? (
              <p role="alert" className="text-[12px] text-danger">
                {errors.consent}
              </p>
            ) : null}
          </div>
        </div>
      )}
    </Dialog>
  );
}

function UploadArea({ onFile, disabled }: { onFile: (file: File | null) => void; disabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const pick = () => !disabled && input.current?.click();
  return (
    <>
      <div
        role="button"
        tabIndex={0}
        aria-label="Choose a voice sample file"
        aria-disabled={disabled || undefined}
        onClick={pick}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            pick();
          }
        }}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e: DragEvent) => {
          e.preventDefault();
          setDragging(false);
          if (!disabled) onFile(e.dataTransfer.files?.[0] ?? null);
        }}
        className={cn(
          "flex cursor-pointer flex-col items-center justify-center gap-1.5 rounded-lg border border-dashed border-line-strong bg-surface-2/50 px-4 py-6 text-center transition-colors",
          "hover:border-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
          dragging && "border-accent bg-accent-soft/60",
          disabled && "pointer-events-none opacity-60",
        )}
      >
        <CloudUpload className="size-5 text-muted" aria-hidden />
        <span className="text-[13px] text-ink-2">Drop a file here or click to choose</span>
        <span className="text-[12px] text-muted">WAV, MP3, M4A, OGG, WebM or FLAC · 5 to 30 seconds · up to 10 MB</span>
      </div>
      <input
        ref={input}
        type="file"
        accept={SAMPLE_ACCEPT}
        hidden
        data-testid="voice-sample-input"
        onChange={(e) => {
          onFile(e.target.files?.[0] ?? null);
          e.target.value = "";
        }}
      />
    </>
  );
}

function SamplePreview({ sample, onRemove }: { sample: VoiceSample; onRemove: () => void }) {
  const [playing, setPlaying] = useState(false);
  const audio = useRef<HTMLAudioElement | null>(null);
  const url = useRef<string | null>(null);

  useEffect(
    () => () => {
      audio.current?.pause();
      if (url.current) URL.revokeObjectURL(url.current);
      url.current = null;
      audio.current = null;
    },
    [sample],
  );

  const playable = sample.wav.size > 0;
  const toggle = () => {
    if (!playable) return;
    if (playing) {
      audio.current?.pause();
      setPlaying(false);
      return;
    }
    if (!audio.current) {
      url.current = URL.createObjectURL(sample.wav);
      audio.current = new Audio(url.current);
      audio.current.onended = () => setPlaying(false);
    }
    void audio.current.play();
    setPlaying(true);
  };

  return (
    <div className="flex items-center gap-3 rounded-md border border-line bg-surface px-3 py-2">
      <Button type="button" size="icon-sm" variant="secondary" aria-label={playing ? "Pause the sample" : "Play the sample"} aria-pressed={playing} onClick={toggle} disabled={!playable}>
        {playing ? <Pause /> : <Play />}
      </Button>
      <FileAudio className="size-4 shrink-0 text-muted" aria-hidden />
      <span className="min-w-0 flex-1 truncate text-[13px]">
        <span className="font-medium">{sample.label}</span> <span className="font-mono text-[12px] text-muted">· {formatSeconds(sample.seconds)}</span>
      </span>
      <Button type="button" size="icon-sm" variant="ghost" aria-label="Remove the sample" onClick={onRemove}>
        <X />
      </Button>
    </div>
  );
}
