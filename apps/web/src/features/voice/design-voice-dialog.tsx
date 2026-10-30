/**
 * "Design a voice" for connections whose server can (`useVoiceCapabilities`): a name, the
 * voice in plain words and, when the server lists its languages, one of them. "Preview"
 * makes the voice and plays the server's sample of it; "Done" keeps it and selects it.
 * Nothing is kept until then: closing the dialog discards the previewed voice.
 */
import { useMutation } from "@tanstack/react-query";
import { Loader2, Sparkles } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Input, Label, Textarea } from "@/components/ui/primitives";
import { Select, SelectItem } from "@/components/ui/select";
import { useInvalidate } from "@/features/queries";
import { fieldError, formError } from "@/features/voice/ai-servers";
import {
  deleteVoice,
  DESCRIPTION_EXAMPLES,
  designVoice,
  fetchVoiceSample,
  MAX_DESCRIPTION_CHARS,
  validateDesign,
  type DesignErrors,
  type PocketVoice,
  type VoiceCapabilities,
} from "@/features/voice/voice-cloning";
import { MAX_NAME_CHARS } from "@/features/voice/voice-sample";

const FIELDS = ["name", "description", "language"] as const;

export function DesignVoiceDialog({
  open,
  onOpenChange,
  connectionId,
  capabilities,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  connectionId: string;
  capabilities: VoiceCapabilities;
  onCreated?: (voice: PocketVoice) => void;
}) {
  const invalidate = useInvalidate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [language, setLanguage] = useState("");
  const [submitted, setSubmitted] = useState(false);
  const [preview, setPreview] = useState<{ voice: PocketVoice; url: string } | null>(null);
  const audio = useRef<HTMLAudioElement | null>(null);
  const kept = useRef(false);
  const latest = useRef(preview);
  latest.current = preview;

  // The object URL and the previewed voice (unless kept) go when the dialog does.
  useEffect(
    () => () => {
      const last = latest.current;
      if (!last) return;
      if (last.url) URL.revokeObjectURL(last.url);
      if (!kept.current) void deleteVoice(connectionId, last.voice.id).catch(() => undefined);
    },
    [connectionId],
  );

  const make = useMutation({
    mutationFn: async () => {
      // Another go at it: the earlier preview is replaced, not piled up on the server.
      if (preview) await deleteVoice(connectionId, preview.voice.id).catch(() => undefined);
      const voice = await designVoice(connectionId, { name, description, language: language || null });
      const sample = await fetchVoiceSample(connectionId, voice.id).catch(() => null);
      return { voice, sample };
    },
    onSuccess: ({ voice, sample }) => {
      if (preview?.url) URL.revokeObjectURL(preview.url);
      setPreview({ voice, url: sample ? URL.createObjectURL(sample) : "" });
      if (!sample) toast.error("The voice is ready, but its preview couldn't be played.");
    },
  });

  // Plays as soon as a preview arrives; the control stays for playing it again.
  useEffect(() => {
    if (preview?.url && audio.current) void audio.current.play().catch(() => undefined);
  }, [preview]);

  const errors: DesignErrors = submitted ? validateDesign({ name, description }) : {};
  const languages = capabilities.languages ?? [];

  const previewVoice = () => {
    setSubmitted(true);
    if (Object.keys(validateDesign({ name, description })).length) return;
    make.mutate();
  };

  const done = () => {
    if (!preview) return;
    kept.current = true;
    toast.success(`Voice “${preview.voice.name}” is ready and selected. Save to use it for calls.`);
    void invalidate("ai", "pocket-voices", connectionId);
    onCreated?.(preview.voice);
    onOpenChange(false);
  };

  const close = () => {
    if (preview && !kept.current) void invalidate("ai", "pocket-voices", connectionId);
    onOpenChange(false);
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => !o && close()}
      width="max-w-xl"
      title="Design a voice"
      description="Describe the voice you want and the voice server makes it from your words. No recording and no speaker are involved."
      footer={
        <>
          <Button onClick={close} disabled={make.isPending}>
            Cancel
          </Button>
          <Button variant={preview ? "secondary" : "primary"} onClick={previewVoice} loading={make.isPending}>
            {make.isPending ? "Designing…" : preview ? "Preview again" : "Preview"}
          </Button>
          {preview ? (
            <Button variant="primary" onClick={done} disabled={make.isPending}>
              Done
            </Button>
          ) : null}
        </>
      }
    >
      <div className="flex flex-col gap-4">
        {make.error ? <FormError message={formError(make.error, FIELDS)} /> : null}
        <Field label="Name" required error={errors.name ?? fieldError(make.error, "name")} hint="Shown in voice pickers, for example “Maeve (front desk)”.">
          <Input value={name} maxLength={MAX_NAME_CHARS + 20} onChange={(e) => setName(e.target.value)} placeholder="Voice name" autoComplete="off" />
        </Field>
        <Field
          label="Description"
          required
          error={errors.description ?? fieldError(make.error, "description")}
          hint={`${Math.min(description.replace(/\s+/g, " ").trim().length, MAX_DESCRIPTION_CHARS)} of ${MAX_DESCRIPTION_CHARS} characters. Describe a kind of voice (age, gender, accent, warmth, pace), not a real person.`}
        >
          <Textarea value={description} rows={4} maxLength={MAX_DESCRIPTION_CHARS + 100} onChange={(e) => setDescription(e.target.value)} placeholder={DESCRIPTION_EXAMPLES.join("\n")} />
        </Field>
        {languages.length ? (
          <div className="flex flex-col gap-1.5">
            <Label htmlFor="design-voice-language">Language</Label>
            <Select id="design-voice-language" value={language} onValueChange={setLanguage} allowEmpty emptyLabel="Server default" aria-label="Language" invalid={!!fieldError(make.error, "language")}>
              {languages.map((l) => (
                <SelectItem key={l} value={l}>
                  {l}
                </SelectItem>
              ))}
            </Select>
            {fieldError(make.error, "language") ? (
              <p role="alert" className="text-[12px] text-danger">
                {fieldError(make.error, "language")}
              </p>
            ) : null}
          </div>
        ) : null}

        {make.isPending ? (
          <p role="status" className="flex items-center gap-2 text-[12.5px] text-muted">
            <Loader2 className="size-3.5 animate-spin" aria-hidden /> Designing the voice… this can take a little while.
          </p>
        ) : preview ? (
          <div className="flex flex-col gap-2 rounded-lg border border-line bg-surface-2/50 px-3 py-3">
            <div className="flex items-center gap-2 text-[13px] font-semibold">
              <Sparkles className="size-4 text-accent" aria-hidden /> Preview of “{preview.voice.name}”
            </div>
            {preview.url ? <audio ref={audio} controls src={preview.url} aria-label={`Preview of ${preview.voice.name}`} className="w-full" /> : null}
            <p className="text-[12px] text-muted">Like it? Choose Done to keep it. Otherwise change the description and preview again; nothing is kept until you choose Done.</p>
          </div>
        ) : null}
      </div>
    </Dialog>
  );
}
