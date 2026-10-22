/**
 * Call settings: how quickly the agent replies, how long a call may run, words speech
 * recognition should know, background noise, natural sounds (laughs and sighs with an
 * expressive voice), voicemail and screening, agent variables, the website widget and the
 * agent's ID. Agent-level, for guided and flow agents alike.
 *
 * Grouped into sections with a small navigation list (a row of chips on narrow screens),
 * so nothing is buried at the bottom of a long page.
 */
import { AudioLines, BookA, Fingerprint, Gauge, Globe, Laugh, PhoneOff, Plus, Timer, Variable, X, type LucideIcon } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import type { UseFormReturn } from "react-hook-form";

import { PRODUCT_NAME } from "@/components/brand";
import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/form";
import { Input, Label } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { Slider } from "@/components/ui/slider";
import { Switch } from "@/components/ui/switch";
import type { ResponseSpeed } from "@/features/calls/latency-types";
import { cueLabel } from "@/features/calls/sound-cues";
import { SoundCue } from "@/features/calls/speech-text";
import { CopyField } from "@/features/developer/copy-button";
import { RESPONSE_SPEEDS, speedFields, type AgentForm } from "@/features/voice/agent-form";
import { useConnections, useLatencyGuide, useModelSelection } from "@/features/voice/queries";
import { AudioFileField } from "@/features/voice/settings/audio-file-field";
import { NOISE_LEVELS } from "@/features/voice/settings/settings-model";
import { VariablesSection } from "@/features/voice/settings/variables-section";
import { VoicemailSection } from "@/features/voice/settings/voicemail-section";
import { WebsiteSection } from "@/features/voice/settings/website-section";
import { isSelfHostedVoice, useSoundTags } from "@/features/voice/voice-cloning";
import { cn } from "@/lib/utils";

type SectionId = "speed" | "limits" | "recognition" | "noise" | "sounds" | "voicemail" | "variables" | "website" | "identity";
const SECTIONS: { id: SectionId; label: string; icon: LucideIcon }[] = [
  { id: "speed", label: "Response speed", icon: Gauge },
  { id: "limits", label: "Length & turn-taking", icon: Timer },
  { id: "recognition", label: "Words to recognise", icon: BookA },
  { id: "noise", label: "Background noise", icon: AudioLines },
  { id: "sounds", label: "Natural sounds", icon: Laugh },
  { id: "voicemail", label: "Voicemail & screening", icon: PhoneOff },
  { id: "variables", label: "Variables", icon: Variable },
  { id: "website", label: "Add to website", icon: Globe },
  { id: "identity", label: "Agent ID", icon: Fingerprint },
];
const anchor = (id: SectionId) => `call-settings-${id}`;

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

type VoiceSounds = { state: "loading" } | { state: "no_voice" } | { state: "plain" } | { state: "expressive"; tags: string[]; promptTags: string[] };

/** Whether the agent's voice (its own, else the organization's) can make sounds such as [laugh]. */
function useVoiceSounds(form: UseFormReturn<AgentForm>): VoiceSounds {
  const override = form.watch("override_tts");
  const selection = useModelSelection();
  const connections = useConnections();
  const connectionId = override?.connection_id ?? selection.data?.tts?.connection_id ?? null;
  const connection = connections.data?.find((c) => c.id === connectionId);
  const sounds = useSoundTags(isSelfHostedVoice(connection) ? connection.id : null);
  if (!override && selection.isLoading) return { state: "loading" };
  if (!connectionId) return { state: "no_voice" };
  if (!connections.data) return { state: connections.isError ? "plain" : "loading" };
  if (!isSelfHostedVoice(connection)) return { state: "plain" };
  if (!sounds.data) return { state: sounds.isError ? "plain" : "loading" };
  return sounds.data.expressive && sounds.data.prompt_tags.length
    ? { state: "expressive", tags: sounds.data.tags, promptTags: sounds.data.prompt_tags }
    : { state: "plain" };
}

function NaturalSounds({ sounds, on }: { sounds: VoiceSounds; on: boolean }) {
  if (sounds.state === "loading") return <p className="text-[12.5px] text-muted">Checking the agent's voice…</p>;
  if (sounds.state === "no_voice") return <p className="text-[12.5px] text-muted">Choose a voice under AI providers first.</p>;
  if (sounds.state === "plain") {
    return (
      <p className="text-[12.5px] text-muted">
        Not available: the agent's voice can't make these sounds and would read them out, so the agent is told never to write them. Choose an expressive voice (Chatterbox-Turbo) under AI providers or as this agent's own voice to turn this on.
      </p>
    );
  }
  const others = sounds.tags.filter((t) => !sounds.promptTags.includes(t));
  return (
    <div className="flex flex-col gap-2 text-[12.5px]">
      <p className="flex flex-wrap items-center gap-1 text-ink-2" aria-label={`The agent may use: ${sounds.promptTags.map(cueLabel).join(", ")}`}>
        {on ? "The agent may use:" : "Off: the agent is told not to make sounds. With it on, it may use:"}
        {sounds.promptTags.map((t) => (
          <SoundCue key={t} label={cueLabel(t)} />
        ))}
      </p>
      <p className="text-muted">
        At most one per reply, only where it fits, never at the start of a reply.
        {others.length ? ` The voice can also make: ${others.map(cueLabel).join(", ")}.` : ""} Applies when you publish the agent.
      </p>
    </div>
  );
}

/** Highlights the section in view; clicking scrolls to it. */
function useActiveSection(): [SectionId, (id: SectionId) => void] {
  const [active, setActive] = useState<SectionId>("speed");
  const pinned = useRef<number>(0);
  useEffect(() => {
    if (typeof IntersectionObserver === "undefined") return;
    const visible = new Map<SectionId, number>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          const id = entry.target.getAttribute("data-section") as SectionId;
          if (entry.isIntersecting) visible.set(id, entry.boundingClientRect.top);
          else visible.delete(id);
        }
        if (Date.now() < pinned.current) return;
        const first = SECTIONS.find((s) => visible.has(s.id));
        if (first) setActive(first.id);
      },
      { rootMargin: "-80px 0px -45% 0px" },
    );
    for (const s of SECTIONS) {
      const el = document.getElementById(anchor(s.id));
      if (el) observer.observe(el);
    }
    return () => observer.disconnect();
  }, []);
  const go = (id: SectionId) => {
    setActive(id);
    pinned.current = Date.now() + 800; // keep the clicked item highlighted while scrolling
    const el = document.getElementById(anchor(id));
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
    el?.focus({ preventScroll: true });
  };
  return [active, go];
}

function SectionNav({ active, onGo, errors }: { active: SectionId; onGo: (id: SectionId) => void; errors: Partial<Record<SectionId, boolean>> }) {
  return (
    <nav aria-label="Call settings sections" className="md:sticky md:top-[72px] md:self-start">
      <ul className="-mx-1 flex gap-1 overflow-x-auto px-1 pb-1 md:mx-0 md:flex-col md:overflow-visible md:px-0 md:pb-0">
        {SECTIONS.map(({ id, label, icon: Icon }) => (
          <li key={id} className="shrink-0">
            <button
              type="button"
              onClick={() => onGo(id)}
              aria-current={active === id ? "true" : undefined}
              className={cn(
                "flex w-full items-center gap-2 whitespace-nowrap rounded-md px-2.5 py-1.5 text-left text-[12.5px] font-medium text-muted transition-colors hover:bg-surface-2 hover:text-ink",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40",
                active === id && "bg-surface-2 text-ink",
              )}
            >
              <Icon className={cn("size-3.5 shrink-0", active === id ? "text-accent" : "text-faint")} aria-hidden />
              <span className="flex-1">{label}</span>
              {errors[id] ? <span className="size-1.5 rounded-full bg-danger" aria-label="has errors" /> : null}
            </button>
          </li>
        ))}
      </ul>
    </nav>
  );
}

function Row({ id, title, description, control, children }: { id: SectionId; title: string; description: string; control?: ReactNode; children?: ReactNode }) {
  return (
    <section id={anchor(id)} data-section={id} tabIndex={-1} aria-labelledby={`${anchor(id)}-title`} className="scroll-mt-20 rounded-lg border border-line focus:outline-none">
      <div className="flex items-start justify-between gap-4 px-4 py-3">
        <div className="min-w-0">
          <h3 id={`${anchor(id)}-title`} className="text-[13.5px] font-semibold">
            {title}
          </h3>
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

function BackgroundNoise({ form, agentId }: { form: UseFormReturn<AgentForm>; agentId: string }) {
  const ambient = form.watch("call_settings.ambient");
  const percent = Math.round(ambient.volume * 100);
  const setVolume = (v: number) => form.setValue("call_settings.ambient.volume", Math.min(1, Math.max(0, Math.round(v) / 100)), { shouldDirty: true });
  if (!ambient.enabled) return <p className="text-[12.5px] text-muted">Off: calls are silent between replies.</p>;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2.5">
        <div className="flex items-center justify-between">
          <Label id="noise-volume-label">Volume</Label>
          <span className="font-mono text-[12px] text-ink-2" aria-hidden>
            {percent}%
          </span>
        </div>
        <Slider value={percent} min={0} max={100} step={1} onValueChange={setVolume} label="Background noise volume" valueText={`${percent}%`} aria-labelledby="noise-volume-label" />
        <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-muted">
          Quick pick:
          {NOISE_LEVELS.map((l) => {
            const on = percent === Math.round(l.volume * 100);
            return (
              <button
                key={l.label}
                type="button"
                aria-pressed={on}
                onClick={() => setVolume(l.volume * 100)}
                className={cn(
                  "rounded border px-2 py-0.5 text-[12px] transition-colors hover:border-accent hover:text-accent",
                  on ? "border-accent bg-accent/10 text-accent" : "border-line bg-surface text-ink-2",
                )}
              >
                {l.label} <span className="font-mono text-[11px] opacity-70">{Math.round(l.volume * 100)}%</span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="flex flex-col gap-2 border-t border-line pt-4">
        <Label>Sound</Label>
        <AudioFileField
          agentId={agentId}
          purpose="ambient"
          label="Custom background sound"
          value={ambient.audio_id}
          onChange={(v) => form.setValue("call_settings.ambient.audio_id", v, { shouldDirty: true })}
          emptyText="Using the default office ambience. Upload your own sound to replace it:"
          uploadLabel="Upload custom sound"
          resetLabel="Use default office ambience"
        />
      </div>
    </div>
  );
}

const SPEECH_REUSE_HINT = "Plays back lines it has said before, such as the greeting, instead of generating them again.";

export function CallSettingsTab({ form, agentId, canEdit }: { form: UseFormReturn<AgentForm>; agentId: string; canEdit: boolean }) {
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
  const voicemailOn = form.watch("call_settings.voicemail.enabled");
  const ambientOn = form.watch("call_settings.ambient.enabled");
  const naturalSounds = form.watch("call_settings.natural_sounds");
  const sounds = useVoiceSounds(form);
  const expressive = sounds.state === "expressive";
  const [active, go] = useActiveSection();

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

  const errors: Partial<Record<SectionId, boolean>> = {
    limits: Boolean(e?.max_call_seconds || e?.idle_timeout_seconds || e?.turn_start_min_words || e?.smart_turn_stop_seconds),
    recognition: Boolean(e?.key_terms),
    noise: Boolean(e?.ambient),
    voicemail: Boolean(e?.voicemail),
    variables: Boolean(form.formState.errors.variables),
  };

  return (
    <div className="grid gap-4 md:grid-cols-[190px_minmax(0,1fr)]">
      <SectionNav active={active} onGo={go} errors={errors} />
      <div className="flex min-w-0 flex-col gap-4">
        <Row id="speed" title="Response speed" description="How quickly the agent answers once the person stops talking.">
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

        <Row id="limits" title="Length and turn-taking" description="Calls end when the limit is reached or the person stops responding.">
          <div className="flex flex-col gap-5">
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
            {custom ? (
              <div className="grid gap-5 border-t border-line pt-4 sm:grid-cols-2">
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
            ) : (
              <p className="border-t border-line pt-3 text-[12px] text-muted">Turn-taking follows the response speed. Choose Custom under Response speed to set it yourself.</p>
            )}
          </div>
        </Row>

        <Row id="recognition" title="Words to recognise" description="Names and terms speech recognition would otherwise mishear.">
          <KeyTerms form={form} />
        </Row>

        <Row
          id="noise"
          title="Background noise"
          description="A soft sound bed under the agent's voice so pauses feel natural."
          control={<Switch aria-label="Background noise" checked={ambientOn} onCheckedChange={(v) => form.setValue("call_settings.ambient.enabled", v, { shouldDirty: true })} />}
        >
          <BackgroundNoise form={form} agentId={agentId} />
        </Row>

        <Row
          id="sounds"
          title="Natural sounds"
          description="The agent laughs, sighs or gasps where a person naturally would. Needs an expressive voice."
          control={
            <Switch
              aria-label="Natural sounds"
              checked={expressive && naturalSounds}
              disabled={!expressive}
              onCheckedChange={(v) => form.setValue("call_settings.natural_sounds", v, { shouldDirty: true })}
            />
          }
        >
          <NaturalSounds sounds={sounds} on={naturalSounds} />
        </Row>

        <Row
          id="voicemail"
          title="Voicemail & screening"
          description="Spot answering machines, call screeners and phone menus so the agent doesn't pitch to a beep."
          control={<Switch aria-label="Detect voicemail and screening" checked={voicemailOn} onCheckedChange={(v) => form.setValue("call_settings.voicemail.enabled", v, { shouldDirty: true, shouldValidate: true })} />}
        >
          <VoicemailSection form={form} agentId={agentId} />
        </Row>

        <Row id="variables" title="Variables" description="Fixed values any prompt, greeting or message can use as {{name}}. Saved with the agent; publish to use them on calls.">
          <VariablesSection form={form} />
        </Row>

        <Row id="website" title="Add to website" description="A talk-to-us button for your own site that connects visitors to this agent.">
          <WebsiteSection agentId={agentId} canEdit={canEdit} />
        </Row>

        <Row id="identity" title="Agent ID" description={`Identifies this agent in the ${PRODUCT_NAME} API, for example to start calls from your own systems.`}>
          <CopyField value={agentId} label="Agent ID" className="max-w-md" />
        </Row>
      </div>
    </div>
  );
}
