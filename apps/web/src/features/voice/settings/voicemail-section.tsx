/**
 * Voicemail & screening: who answered (a person, voicemail, a call screener or a phone
 * menu), what to do about it, and which model decides.
 */
import { useMutation } from "@tanstack/react-query";
import { ChevronDown, KeyRound } from "lucide-react";
import { useId, useState } from "react";
import type { UseFormReturn } from "react-hook-form";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/form";
import { Input, Label, Textarea } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { Select, SelectItem } from "@/components/ui/select";
import { useInvalidate } from "@/features/queries";
import type { AgentForm } from "@/features/voice/agent-form";
import { useCatalog, useConnections, type Catalog, type Connection } from "@/features/voice/queries";
import { AudioFileField } from "@/features/voice/settings/audio-file-field";
import { useClassifierKey } from "@/features/voice/settings/agent-settings-api";
import { classifierConnections } from "@/features/voice/settings/settings-model";
import { api, errorMessage, unwrap } from "@/lib/api";
import { formatRelative } from "@/lib/format";

const LABELS = "CONVERSATION, VOICEMAIL, NO_MESSAGE, SCREENER, SCREENING_WAIT, IVR or UNKNOWN";

function modelSuggestions(catalog: Catalog | undefined, provider: string | undefined): string[] {
  const schema = catalog?.llm.find((p) => p.id === provider)?.settings_schema as { properties?: Record<string, { enum?: unknown[]; examples?: unknown[] }> } | undefined;
  const model = schema?.properties?.model;
  return [...(model?.enum ?? []), ...(model?.examples ?? [])].filter((m): m is string => typeof m === "string").slice(0, 6);
}

function ClassifierKey({ agentId, connection, disabled }: { agentId: string; connection: Connection | undefined; disabled?: boolean }) {
  const id = useId();
  const [value, setValue] = useState("");
  const invalidate = useInvalidate();
  const stored = useClassifierKey(agentId, true);
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/api/v1/agents/{agent_id}/voicemail-classifier-key", {
          params: { path: { agent_id: agentId } },
          body: { connection_id: connection?.id ?? "", api_key: value },
        }),
      ),
    onSuccess: () => {
      setValue("");
      void invalidate("agent", agentId, "classifier-key");
      toast.success("API key stored. It is used the next time you publish.");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  const matches = stored.data && connection && stored.data.connection_id === connection.id;
  return (
    <div className="flex flex-col gap-1.5">
      <Label htmlFor={id}>API key for {connection?.name ?? "this connection"}</Label>
      <div className="flex gap-2">
        <Input
          id={id}
          type="password"
          autoComplete="off"
          placeholder={matches ? "Stored. Enter a new key to replace it" : "Paste the provider's API key"}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          disabled={disabled || !connection}
          className="font-mono"
        />
        <Button type="button" disabled={disabled || !connection || !value.trim()} loading={save.isPending} onClick={() => save.mutate()}>
          <KeyRound /> {matches ? "Replace key" : "Store key"}
        </Button>
      </div>
      <p className="text-[12px] text-muted">
        {matches && stored.data
          ? `Stored ${formatRelative(stored.data.updated_at)}. `
          : "Not stored yet: publishing needs it. "}
        The voice service runs this model with its own copy of the key, so it is entered here once, kept encrypted and never shown again.
      </p>
    </div>
  );
}

export function VoicemailSection({ form, agentId }: { form: UseFormReturn<AgentForm>; agentId: string }) {
  const vm = form.watch("call_settings.voicemail");
  const e = form.formState.errors.call_settings?.voicemail;
  const set = form.setValue;
  const opts = { shouldDirty: true, shouldValidate: true } as const;
  const catalog = useCatalog();
  const connections = useConnections();
  const eligible = classifierConnections(connections.data);
  const connection = eligible.find((c) => c.id === vm.classifier.connection_id);
  const suggestions = modelSuggestions(catalog.data, connection?.provider);
  const [showInstructions, setShowInstructions] = useState(Boolean(vm.classifier.instructions));
  const instructionsId = useId();

  if (!vm.enabled) return <p className="text-[12.5px] text-muted">Off: the agent treats whatever answers as a person.</p>;

  return (
    <div className="flex flex-col gap-5">
      <div className="flex flex-col gap-3">
        <Label>When voicemail answers</Label>
        <Segmented
          label="When voicemail answers"
          value={vm.action}
          options={[
            { value: "hangup", label: "Hang up" },
            { value: "leave_message", label: "Leave a message" },
          ]}
          onChange={(v) => set("call_settings.voicemail.action", v, opts)}
          className="self-start"
        />
        {vm.action === "leave_message" ? (
          <div className="flex flex-col gap-3 rounded-md border border-line px-3 py-3">
            <Segmented
              size="sm"
              label="Voicemail message as"
              value={vm.message_format}
              options={[
                { value: "text", label: "Spoken text" },
                { value: "recording", label: "Recording" },
              ]}
              onChange={(v) => set("call_settings.voicemail.message_format", v, opts)}
              className="self-start"
            />
            {vm.message_format === "text" ? (
              <Field label="Message" hint="Spoken after the beep, then the call ends. Variables such as {{first_name}} work here." error={e?.message?.message} required>
                <Textarea rows={3} placeholder="Hi {{first_name}}, this is Sarah from Acme. Please call us back at…" {...form.register("call_settings.voicemail.message")} />
              </Field>
            ) : (
              <>
                <AudioFileField
                  agentId={agentId}
                  purpose="message"
                  label="Voicemail recording"
                  value={vm.message_audio_id}
                  onChange={(v) => set("call_settings.voicemail.message_audio_id", v, opts)}
                  emptyText="Played after the beep, then the call ends."
                  uploadLabel="Upload recording"
                  error={e?.message_audio_id?.message}
                />
                <Field label="What the recording says" hint="Optional. Kept with the call transcript." error={e?.message?.message}>
                  <Textarea rows={2} {...form.register("call_settings.voicemail.message")} />
                </Field>
              </>
            )}
          </div>
        ) : null}
      </div>

      <div className="flex flex-col gap-3 border-t border-line pt-4">
        <div>
          <Label>When a call screener answers</Label>
          <p className="mt-0.5 text-[12px] text-muted">Phones that ask who is calling. The agent says who it is and why, then waits for the person.</p>
        </div>
        <Segmented
          size="sm"
          label="Screening message as"
          value={vm.screening_format}
          options={[
            { value: "text", label: "Spoken text" },
            { value: "recording", label: "Recording" },
          ]}
          onChange={(v) => set("call_settings.voicemail.screening_format", v, opts)}
          className="self-start"
        />
        {vm.screening_format === "text" ? (
          <Field label="What to say" hint="Leave empty to hang up on screened calls." error={e?.screening_message?.message}>
            <Textarea rows={2} placeholder="This is Sarah from Acme, calling about your enquiry." {...form.register("call_settings.voicemail.screening_message")} />
          </Field>
        ) : (
          <AudioFileField
            agentId={agentId}
            purpose="message"
            label="Screening recording"
            value={vm.screening_audio_id}
            onChange={(v) => set("call_settings.voicemail.screening_audio_id", v, opts)}
            emptyText="Played to the screener before waiting."
            uploadLabel="Upload recording"
            error={e?.screening_audio_id?.message}
          />
        )}
        <Field label="Wait for the person" hint="Seconds after the screening message, 1 to 60." error={e?.screening_wait_seconds?.message}>
          <Input type="number" inputMode="numeric" min={1} max={60} className="w-28 font-mono" {...form.register("call_settings.voicemail.screening_wait_seconds", { valueAsNumber: true })} />
        </Field>
      </div>

      <div className="flex flex-col gap-3 border-t border-line pt-4">
        <div>
          <Label>Detection model</Label>
          <p className="mt-0.5 text-[12px] text-muted">Decides from the first words whether a person, voicemail, a screener or a phone menu answered.</p>
        </div>
        <Segmented
          size="sm"
          label="Detection model"
          value={vm.classifier.model}
          options={[
            { value: "agent", label: "The agent's language model" },
            { value: "connection", label: "A separate model" },
          ]}
          onChange={(v) => set("call_settings.voicemail.classifier.model", v, opts)}
          className="self-start"
        />
        {vm.classifier.model === "connection" ? (
          <div className="flex flex-col gap-3 rounded-md border border-line px-3 py-3">
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Connection" hint="From AI providers: OpenAI, Groq, Google or OpenRouter." error={e?.classifier?.connection_id?.message}>
                <Select
                  value={vm.classifier.connection_id}
                  onValueChange={(v) => set("call_settings.voicemail.classifier.connection_id", v, opts)}
                  placeholder={eligible.length ? "Choose a connection" : "No eligible connection"}
                  disabled={!eligible.length}
                >
                  {eligible.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name} · {c.provider_title}
                    </SelectItem>
                  ))}
                </Select>
              </Field>
              <Field label="Model" hint="A fast, inexpensive model is enough." error={e?.classifier?.model_name?.message}>
                <Input className="font-mono" placeholder={suggestions[0] ?? "gpt-4.1-mini"} {...form.register("call_settings.voicemail.classifier.model_name")} />
              </Field>
            </div>
            {suggestions.length ? (
              <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-muted">
                Suggested:
                {suggestions.map((m) => (
                  <button
                    key={m}
                    type="button"
                    onClick={() => set("call_settings.voicemail.classifier.model_name", m, opts)}
                    className="rounded border border-line bg-surface px-1.5 py-0.5 font-mono text-[11.5px] text-ink-2 hover:border-accent hover:text-accent"
                  >
                    {m}
                  </button>
                ))}
              </div>
            ) : null}
            <ClassifierKey agentId={agentId} connection={connection} />
          </div>
        ) : null}

        <div>
          <Button type="button" size="sm" variant="ghost" aria-expanded={showInstructions} aria-controls={instructionsId} onClick={() => setShowInstructions((v) => !v)}>
            <ChevronDown className={showInstructions ? "rotate-180 transition-transform" : "transition-transform"} /> Classifier instructions
            {vm.classifier.instructions.trim() ? <span className="ml-1 text-[11px] text-accent">custom</span> : null}
          </Button>
          {showInstructions ? (
            <div id={instructionsId} className="mt-2">
              <Field
                label="Instructions"
                hint={`Leave empty to keep the built-in instructions as they improve. Edit them when calls are not in English: describe the greetings and carrier announcements your callers hear. The reply must be one label: ${LABELS}. Anything else counts as UNKNOWN, which lets the call through.`}
                error={e?.classifier?.instructions?.message}
              >
                <Textarea rows={6} maxLength={8000} placeholder="Leave empty to use the built-in instructions." {...form.register("call_settings.voicemail.classifier.instructions")} />
              </Field>
            </div>
          ) : null}
        </div>
      </div>
      <p className="text-[12px] text-muted">Applies with separate speech and language models; speech-to-speech models handle answering on their own.</p>
    </div>
  );
}
