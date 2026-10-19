/**
 * Call settings: how quickly the agent replies, how long a call may run, which words speech
 * recognition should know, what to do on voicemail, and background ambience. Agent-level,
 * for guided and flow agents alike.
 */
import { Plus, X } from "lucide-react";
import { useState } from "react";
import type { UseFormReturn } from "react-hook-form";

import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/form";
import { Input, Label, Textarea } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { Select, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type { ResponseSpeed } from "@/features/calls/latency-types";
import { RESPONSE_SPEEDS, speedFields, type AgentForm } from "@/features/voice/agent-form";
import { useConnections, useLatencyGuide, useModelSelection } from "@/features/voice/queries";

const AMBIENCE_LEVELS: { value: string; label: string; volume: number }[] = [
  { value: "quiet", label: "Quiet", volume: 0.15 },
  { value: "medium", label: "Medium", volume: 0.3 },
  { value: "loud", label: "Noticeable", volume: 0.5 },
];

/** `true`/`false` once the voice in use is known; `null` while loading or when no voice is chosen. */
function useTtsCacheSupported(form: UseFormReturn<AgentForm>): boolean | null {
  const override = form.watch("override_tts");
  const selection = useModelSelection();
  const connections = useConnections();
  // The latency guide lists the voice providers that can replay audio already generated for the same text.
  const guide = useLatencyGuide();
  const connectionId = override?.connection_id ?? selection.data?.tts?.connection_id;
  if (!connectionId || !connections.data || !guide.data) return null;
  const provider = connections.data.find((c) => c.id === connectionId)?.provider;
  return provider ? guide.data.tts_cache_providers.includes(provider) : null;
}

function Row({ title, description, control, children }: { title: string; description: string; control?: React.ReactNode; children?: React.ReactNode }) {
  return (
    <section className="rounded-lg border border-line">
      <div className="flex items-start justify-between gap-4 px-4 py-3">
        <div className="min-w-0">
          <h3 className="text-[13.5px] font-semibold">{title}</h3>
          <p className="mt-0.5 text-[12.5px] text-muted">{description}</p>
        </div>
        {control}
      </div>
      {children ? <div className="border-t border-line px-4 py-4">{children}</div> : null}
    </section>
  );
}

function Toggle({ label, description, checked, onChange, disabled }: { label: string; description: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <div className="flex items-start justify-between gap-4">
      <div className="min-w-0">
        <p className="text-[13px] font-medium text-ink">{label}</p>
        <p className="mt-0.5 text-[12.5px] text-muted">{description}</p>
      </div>
      <Switch aria-label={label} checked={checked} onCheckedChange={onChange} disabled={disabled} className="mt-0.5" />
    </div>
  );
}

function KeyTerms({ form }: { form: UseFormReturn<AgentForm> }) {
  const [draft, setDraft] = useState("");
  const terms = form.watch("call_settings.key_terms");
  const set = (next: string[]) => form.setValue("call_settings.key_terms", next, { shouldDirty: true, shouldValidate: true });
  const add = () => {
    const cleaned = draft.replace(/,/g, " ").trim().replace(/\s+/g, " ");
    if (!cleaned || terms.includes(cleaned) || terms.length >= 100 || cleaned.length > 60) return;
    set([...terms, cleaned]);
    setDraft("");
  };
  return (
    <div className="flex flex-col gap-2">
      <div className="flex gap-2">
        <Input
          aria-label="New key term"
          placeholder="Acme Realty, Hina, Gulberg…"
          value={draft}
          maxLength={60}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === ",") {
              e.preventDefault();
              add();
            }
          }}
        />
        <Button type="button" onClick={add} disabled={!draft.trim() || terms.length >= 100}>
          <Plus /> Add
        </Button>
      </div>
      {terms.length ? (
        <ul className="flex flex-wrap gap-1.5" aria-label="Key terms">
          {terms.map((t) => (
            <li key={t} className="inline-flex h-6 items-center gap-1 rounded-[4px] border border-line bg-surface-2 px-1.5 text-[12px] text-ink-2">
              {t}
              <button type="button" aria-label={`Remove ${t}`} onClick={() => set(terms.filter((x) => x !== t))} className="grid size-4 place-items-center rounded text-faint hover:text-ink">
                <X className="size-3" />
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[12px] text-faint">Nothing yet. Names, products and places the person is likely to say.</p>
      )}
      <p className="text-[12px] text-muted">{terms.length} / 100</p>
    </div>
  );
}

const SPEECH_REUSE_HINT = "Plays back lines it has said before, such as the greeting, instead of generating them again.";

export function CallSettingsTab({ form }: { form: UseFormReturn<AgentForm> }) {
  const e = form.formState.errors.call_settings;
  const speed = form.watch("call_settings.response_speed");
  const custom = speed === "custom";
  const preset = speedFields(speed);
  const compaction = form.watch("call_settings.context_compaction");
  const ttsCache = form.watch("call_settings.tts_cache");
  const cacheSupported = useTtsCacheSupported(form);
  const cacheUnavailable = cacheSupported === false && !ttsCache;
  const turnStart = form.watch("call_settings.turn_start");
  const turnStop = form.watch("call_settings.turn_stop");
  const voicemail = form.watch("call_settings.voicemail");
  const ambient = form.watch("call_settings.ambient");
  const ambienceLevel = [...AMBIENCE_LEVELS].sort((a, b) => Math.abs(a.volume - ambient.volume) - Math.abs(b.volume - ambient.volume))[0]?.value ?? "medium";

  const chooseSpeed = (next: ResponseSpeed) => {
    const opts = { shouldDirty: true, shouldValidate: true } as const;
    form.setValue("call_settings.response_speed", next, opts);
    // A preset keeps the explicit fields in step with it; switching to Custom starts from the preset that was on.
    const fields = speedFields(next) ?? preset;
    if (!fields) return;
    form.setValue("call_settings.turn_start", fields.turn_start, opts);
    form.setValue("call_settings.turn_start_min_words", fields.turn_start_min_words, opts);
    form.setValue("call_settings.turn_stop", fields.turn_stop, opts);
    form.setValue("call_settings.smart_turn_stop_seconds", fields.smart_turn_stop_seconds, opts);
    form.setValue("call_settings.idle_timeout_seconds", fields.idle_timeout_seconds, opts);
  };

  return (
    <div className="flex flex-col gap-4">
      <Row title="Response speed" description="How quickly the agent answers once the person stops talking.">
        <div className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Segmented<ResponseSpeed> label="Response speed" value={speed} options={RESPONSE_SPEEDS.map((s) => ({ value: s.value, label: s.label }))} onChange={chooseSpeed} className="self-start" />
            <p className="text-[12.5px] text-ink-2" aria-live="polite">
              {RESPONSE_SPEEDS.find((s) => s.value === speed)?.description}
            </p>
            {preset ? (
              <p className="text-[12px] text-muted">
                Hangs up after <span className="font-mono text-ink-2">{preset.idle_timeout_seconds} s</span> of silence. Choose Custom to set turn-taking and the silence limit yourself.
              </p>
            ) : null}
          </div>
          <div className="flex flex-col gap-3 border-t border-line pt-4">
            <Toggle
              label="Shorter memory on long calls"
              description="Condenses the early part of a long conversation so replies stay quick. The agent keeps the gist, not every word."
              checked={compaction}
              onChange={(v) => form.setValue("call_settings.context_compaction", v, { shouldDirty: true })}
            />
            <Toggle
              label="Reuse generated speech"
              description={`${SPEECH_REUSE_HINT} ${cacheUnavailable ? "Not available with the voice provider in use." : "Only some voice providers support this."}`}
              checked={ttsCache}
              disabled={cacheUnavailable}
              onChange={(v) => form.setValue("call_settings.tts_cache", v, { shouldDirty: true })}
            />
          </div>
        </div>
      </Row>

      <Row title="Length and silence" description="Calls end when the limit is reached or the person stops responding.">
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Longest call" hint="Seconds. 30 to 1200." error={e?.max_call_seconds?.message}>
            <Input type="number" inputMode="numeric" min={30} max={1200} step={30} className="font-mono" {...form.register("call_settings.max_call_seconds", { valueAsNumber: true })} />
          </Field>
          {custom ? (
            <Field label="Hang up after silence" hint="Seconds without the person speaking. 3 to 60." error={e?.idle_timeout_seconds?.message}>
              <Input type="number" inputMode="numeric" min={3} max={60} className="font-mono" {...form.register("call_settings.idle_timeout_seconds", { valueAsNumber: true })} />
            </Field>
          ) : null}
        </div>
      </Row>

      {custom ? (
        <Row title="Turn-taking" description="When the agent treats the person as having started and finished speaking.">
          <div className="grid gap-5 sm:grid-cols-2">
            <div className="flex flex-col gap-2">
              <Label>The person starts talking</Label>
              <Segmented
                label="When the person starts talking"
                value={turnStart}
                options={[
                  { value: "default", label: "On any speech" },
                  { value: "min_words", label: "After a few words" },
                ]}
                onChange={(v) => form.setValue("call_settings.turn_start", v, { shouldDirty: true })}
              />
              {turnStart === "min_words" ? (
                <Field label="Words before it counts" hint="1 to 10. Filters out 'mm-hm' while the agent talks." error={e?.turn_start_min_words?.message}>
                  <Input type="number" inputMode="numeric" min={1} max={10} className="w-28 font-mono" {...form.register("call_settings.turn_start_min_words", { valueAsNumber: true })} />
                </Field>
              ) : null}
            </div>
            <div className="flex flex-col gap-2">
              <Label>The person has finished</Label>
              <Segmented
                label="When the person has finished"
                value={turnStop}
                options={[
                  { value: "transcription", label: "When they pause" },
                  { value: "turn_analyzer", label: "Smart detection" },
                ]}
                onChange={(v) => form.setValue("call_settings.turn_stop", v, { shouldDirty: true })}
              />
              {turnStop === "turn_analyzer" ? (
                <Field label="Longest pause to wait" hint="Seconds, 0.5 to 5. Smart detection waits up to this long for the sentence to end." error={e?.smart_turn_stop_seconds?.message}>
                  <Input type="number" inputMode="decimal" min={0.5} max={5} step={0.5} className="w-28 font-mono" {...form.register("call_settings.smart_turn_stop_seconds", { valueAsNumber: true })} />
                </Field>
              ) : null}
            </div>
          </div>
        </Row>
      ) : null}

      <Row title="Words to recognise" description="Names and terms speech recognition would otherwise mishear.">
        <KeyTerms form={form} />
      </Row>

      <Row
        title="Voicemail"
        description="Spot answering machines so the agent doesn't pitch to a beep."
        control={<Switch aria-label="Detect voicemail" checked={voicemail.enabled} onCheckedChange={(v) => form.setValue("call_settings.voicemail.enabled", v, { shouldDirty: true, shouldValidate: true })} />}
      >
        {voicemail.enabled ? (
          <div className="flex flex-col gap-3">
            <Segmented
              label="On voicemail"
              value={voicemail.action}
              options={[
                { value: "hangup", label: "Hang up" },
                { value: "leave_message", label: "Leave a message" },
              ]}
              onChange={(v) => form.setValue("call_settings.voicemail.action", v, { shouldDirty: true, shouldValidate: true })}
            />
            {voicemail.action === "leave_message" ? (
              <Field label="Message" hint="Spoken after the beep. Variables such as {{first_name}} work here." error={e?.voicemail?.message?.message} required>
                <Textarea rows={3} {...form.register("call_settings.voicemail.message")} />
              </Field>
            ) : null}
          </div>
        ) : (
          <p className="text-[12.5px] text-muted">Off: the agent treats whatever answers as a person.</p>
        )}
      </Row>

      <Row
        title="Background ambience"
        description="A faint office sound bed so silences feel less synthetic."
        control={<Switch aria-label="Background ambience" checked={ambient.enabled} onCheckedChange={(v) => form.setValue("call_settings.ambient.enabled", v, { shouldDirty: true })} />}
      >
        {ambient.enabled ? (
          <Field label="Level">
            <Select
              aria-label="Ambience level"
              className="w-48"
              value={ambienceLevel}
              onValueChange={(v) => form.setValue("call_settings.ambient.volume", AMBIENCE_LEVELS.find((l) => l.value === v)?.volume ?? 0.3, { shouldDirty: true })}
            >
              {AMBIENCE_LEVELS.map((l) => (
                <SelectItem key={l.value} value={l.value}>
                  {l.label}
                </SelectItem>
              ))}
            </Select>
          </Field>
        ) : (
          <p className="text-[12.5px] text-muted">Off: calls are silent between replies.</p>
        )}
      </Row>
    </div>
  );
}
