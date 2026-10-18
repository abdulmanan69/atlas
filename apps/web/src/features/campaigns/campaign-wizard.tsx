import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Check, Lock, Rocket, ShieldAlert } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Controller, useForm } from "react-hook-form";
import { Link, useNavigate } from "react-router";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { FormCombobox } from "@/components/ui/combobox";
import { FormDateTimePicker } from "@/components/ui/date-time-picker";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Avatar, Badge, Checkbox, Input, Label, Skeleton, Textarea } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { FormSelect, SelectItem } from "@/components/ui/select";
import { CallingHoursEditor } from "@/features/campaigns/calling-hours-editor";
import {
  audienceFilter,
  createBody,
  isLocked,
  MODE_BADGE,
  skipReasonLabel,
  STEP_FIELDS,
  stepPatch,
  summarizeHours,
  wizardDefaults,
  wizardSchema,
  type CampaignMode,
  type RetryTrigger,
  type WizardInput,
  type WizardOutput,
} from "@/features/campaigns/campaigns-model";
import { qk, useAgents, useInvalidate, useLeads, useMembers, useOrgId, useStages, type Campaign } from "@/features/queries";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { formatDateTime, formatNumber } from "@/lib/format";
import { useCurrentMembership, useHasRole } from "@/lib/session";
import { cn, TIMEZONE_OPTIONS } from "@/lib/utils";

const STEPS = ["Basics", "Audience", "Schedule", "Pacing & retries", "Review"] as const;
const RETRY_TRIGGERS: { id: RetryTrigger; label: string; hint: string }[] = [
  { id: "no_answer", label: "No answer", hint: "Nobody picked up" },
  { id: "busy", label: "Busy", hint: "The line was engaged" },
  { id: "failed", label: "Failed", hint: "The call could not be placed" },
];

/**
 * Five steps. The draft is created on the API when Basics and Audience are both known
 * (the API requires an audience to create a campaign) and patched after every later step,
 * so the Review step can ask the server for a real preview. Editing an existing campaign
 * walks the same steps with the fields the API freezes after start shown locked.
 */
export function CampaignWizard({ open, onOpenChange, campaign }: { open: boolean; onOpenChange: (open: boolean) => void; campaign?: Campaign }) {
  const navigate = useNavigate();
  const invalidate = useInvalidate();
  const orgTz = useCurrentMembership()?.organization.timezone ?? "UTC";
  const [step, setStep] = useState(0);
  const [draft, setDraft] = useState<Campaign | undefined>(campaign);
  const locked = draft ? isLocked(draft.status) : false;

  const form = useForm<WizardInput, unknown, WizardOutput>({
    resolver: zodResolver(wizardSchema),
    defaultValues: wizardDefaults(orgTz, campaign),
    mode: "onTouched",
  });

  useEffect(() => {
    if (!open) return;
    setStep(0);
    setDraft(campaign);
    form.reset(wizardDefaults(orgTz, campaign));
  }, [open, campaign, orgTz, form]);

  const save = useMutation({
    mutationFn: async ({ at, values }: { at: number; values: WizardOutput }) => {
      if (!draft) return unwrap(api.POST("/api/v1/campaigns", { body: createBody(values) }));
      const body = stepPatch(at, values, locked, draft.calling_hours);
      if (Object.keys(body).length === 0) return draft;
      return unwrap(api.PATCH("/api/v1/campaigns/{campaign_id}", { params: { path: { campaign_id: draft.id } }, body }));
    },
    onSuccess: (saved) => {
      setDraft(saved);
      void invalidate("campaigns");
      void invalidate("campaign", saved.id);
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 422) {
        for (const [k, m] of Object.entries(err.fieldErrors())) {
          if (k in wizardSchema.shape) form.setError(k as keyof WizardInput, { message: m });
        }
      }
    },
  });

  const next = async () => {
    const fields = STEP_FIELDS[step] ?? [];
    if (!(await form.trigger(fields as (keyof WizardInput)[]))) return;
    const parsed = wizardSchema.safeParse(form.getValues());
    // Before the audience exists nothing can be persisted; later steps always see a valid whole.
    if (parsed.success && (draft || step >= 1)) {
      try {
        await save.mutateAsync({ at: step, values: parsed.data });
      } catch {
        return;
      }
    }
    setStep((s) => Math.min(s + 1, STEPS.length - 1));
  };

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next && draft && !campaign) toast.success(`“${draft.name}” saved as a draft`);
  };

  const e = form.formState.errors;
  const title = campaign ? `Edit ${campaign.name}` : "New campaign";

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title={title}
      description={campaign && locked ? "The agent, mode and audience were frozen when the campaign started." : "An AI agent calls an audience of leads under explicit pacing, calling-hour and retry rules."}
      width="max-w-2xl"
      footer={
        <>
          <Button variant="ghost" onClick={() => close(false)}>
            {draft && step < STEPS.length - 1 ? "Save draft & close" : "Close"}
          </Button>
          <span className="flex-1" />
          <Button disabled={step === 0 || save.isPending} onClick={() => setStep((s) => s - 1)}>
            Back
          </Button>
          {step < STEPS.length - 1 ? (
            <Button variant="primary" loading={save.isPending} onClick={() => void next()}>
              {step === 1 && !draft ? "Create draft & continue" : "Continue"}
            </Button>
          ) : draft ? (
            <ReviewActions draft={draft} onClose={() => close(false)} onStarted={(c) => void navigate(`/campaigns/${c.id}`)} />
          ) : null}
        </>
      }
    >
      <Stepper step={step} />
      {save.error && !(save.error instanceof ApiError && save.error.status === 422) ? (
        <div className="mb-4">
          <FormError message={errorMessage(save.error)} />
        </div>
      ) : null}
      <form noValidate onSubmit={(ev) => ev.preventDefault()} className="flex flex-col gap-4">
        {step === 0 ? <BasicsStep form={form} locked={locked} draft={draft} /> : null}
        {step === 1 ? <AudienceStep form={form} locked={locked} /> : null}
        {step === 2 ? (
          <>
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Start" hint="Leave empty to start as soon as you launch." error={e.start_at?.message}>
                <FormDateTimePicker control={form.control} name="start_at" clearable placeholder="As soon as started" />
              </Field>
              <Field label="End" hint="Leave empty to run until every lead is done." error={e.end_at?.message}>
                <FormDateTimePicker control={form.control} name="end_at" clearable placeholder="When done" />
              </Field>
            </div>
            <Field label="Time zone" hint="Used for leads that have no time zone of their own." error={e.timezone?.message}>
              <FormCombobox control={form.control} name="timezone" options={TIMEZONE_OPTIONS} searchPlaceholder="Search time zones" />
            </Field>
            <div className="flex flex-col gap-1.5">
              <Label>Calling hours</Label>
              <Controller control={form.control} name="hours" render={({ field, fieldState }) => <CallingHoursEditor value={field.value} onChange={field.onChange} error={fieldState.error?.message ?? null} />} />
              <p className="text-[12px] text-muted">Evaluated in each lead's time zone. Leads outside their window wait for the next opening without blocking others.</p>
            </div>
          </>
        ) : null}
        {step === 3 ? <PacingStep form={form} /> : null}
        {step === 4 && draft ? <ReviewStep draft={draft} values={form.getValues()} /> : null}
      </form>
    </Dialog>
  );
}

function Stepper({ step }: { step: number }) {
  return (
    <ol className="mb-5 flex items-center gap-1 text-[12px]" aria-label="Steps">
      {STEPS.map((label, i) => {
        const state = i < step ? "done" : i === step ? "current" : "todo";
        return (
          <li key={label} className="flex items-center gap-1" aria-current={state === "current" ? "step" : undefined}>
            <span
              className={cn(
                "inline-flex h-6 items-center gap-1.5 rounded-full px-2 font-medium",
                state === "current" && "bg-accent-soft text-accent-soft-ink",
                state === "done" && "text-success",
                state === "todo" && "text-faint",
              )}
            >
              <span className={cn("grid size-4 place-items-center rounded-full text-[10px]", state === "done" ? "bg-success text-white" : state === "current" ? "bg-accent text-accent-ink" : "bg-surface-2")}>
                {state === "done" ? <Check className="size-2.5" strokeWidth={3} /> : i + 1}
              </span>
              <span className="hidden sm:inline">{label}</span>
            </span>
            {i < STEPS.length - 1 ? <span aria-hidden className="h-px w-3 bg-line" /> : null}
          </li>
        );
      })}
    </ol>
  );
}

type FormApi = ReturnType<typeof useForm<WizardInput, unknown, WizardOutput>>;

function BasicsStep({ form, locked, draft }: { form: FormApi; locked: boolean; draft?: Campaign }) {
  const agents = useAgents();
  const ready = useMemo(() => (agents.data ?? []).filter((a) => a.status === "active" && a.published_version !== null), [agents.data]);
  const e = form.formState.errors;
  const current = agents.data?.find((a) => a.id === draft?.agent_id);
  return (
    <>
      <Field label="Name" required error={e.name?.message}>
        <Input autoFocus placeholder="October re-engagement" {...form.register("name")} />
      </Field>
      <Field label="Objective" hint="What a successful call achieves. Shown to your team, not to the agent." error={e.objective?.message}>
        <Textarea placeholder="Re-qualify leads that went quiet and book a viewing" {...form.register("objective")} />
      </Field>
      {locked ? (
        <Field label="Agent" hint="Frozen once the campaign has started.">
          <div className="flex h-8 items-center gap-2 text-[13px]">
            <Lock className="size-3.5 text-faint" aria-hidden /> {current?.name ?? "Agent"}
          </div>
        </Field>
      ) : (
        <Field
          label="Agent"
          required
          hint={
            agents.data && ready.length === 0 ? (
              <>
                No agent is published and active.{" "}
                <Link to="/agents" className="font-medium text-accent hover:underline">
                  Publish one first
                </Link>
                .
              </>
            ) : (
              "Only published, active agents can run a campaign."
            )
          }
          error={e.agent_id?.message}
        >
          <FormSelect control={form.control} name="agent_id" placeholder="Choose an agent" disabled={ready.length === 0}>
            {ready.map((a) => (
              <SelectItem key={a.id} value={a.id} description={`v${a.published_version} published · ${a.direction}`}>
                {a.name}
              </SelectItem>
            ))}
          </FormSelect>
        </Field>
      )}
    </>
  );
}

function AudienceStep({ form, locked }: { form: FormApi; locked: boolean }) {
  const stages = useStages();
  const members = useMembers();
  const e = form.formState.errors;
  const watched = form.watch(["stage_id", "owner", "tag", "source", "qualification_status"]);
  const [stage_id, owner, tag, source, qualification_status] = watched;
  const spec = audienceFilter({ stage_id, owner, tag: tag.trim().toLowerCase(), source: source.trim(), qualification_status });
  const any = Boolean(stage_id || owner || tag.trim() || source.trim() || qualification_status);
  const base = {
    stage_id: spec.stage_id ?? undefined,
    owner_user_id: spec.owner_user_id ?? undefined,
    unassigned: spec.unassigned || undefined,
    tag: spec.tag ?? undefined,
    source: spec.source ?? undefined,
    qualification_status: spec.qualification_status ?? undefined,
    page_size: 1,
  };
  const matched = useLeads(any ? base : { page_size: 1 });
  const callable = useLeads(any ? { ...base, do_not_call: false } : { page_size: 1, do_not_call: false });

  const ownerOptions = useMemo(
    () => [
      { value: "unassigned", label: "Unassigned" },
      ...(members.data ?? []).map((m) => ({ value: m.user.id, label: m.user.full_name, icon: <Avatar name={m.user.full_name} className="size-4 text-[8px]" /> })),
    ],
    [members.data],
  );

  return (
    <>
      {locked ? (
        <p className="flex items-center gap-1.5 rounded-md border border-line bg-surface-2/60 px-3 py-2 text-[12.5px] text-muted">
          <Lock className="size-3.5" aria-hidden /> The audience was snapshotted when the campaign started and can't be changed.
        </p>
      ) : null}
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Stage" error={e.stage_id?.message}>
          <FormSelect control={form.control} name="stage_id" allowEmpty emptyLabel="Any stage" disabled={locked}>
            {stages.data?.map((s) => (
              <SelectItem key={s.id} value={s.id}>
                {s.name}
              </SelectItem>
            ))}
          </FormSelect>
        </Field>
        <Field label="Owner">
          <FormCombobox control={form.control} name="owner" options={ownerOptions} placeholder="Any owner" searchPlaceholder="Search teammates" allowClear disabled={locked} />
        </Field>
        <Field label="Tag" error={e.tag?.message}>
          <Input placeholder="q4" {...form.register("tag")} disabled={locked} />
        </Field>
        <Field label="Source" error={e.source?.message}>
          <Input placeholder="web_form" className="font-mono" {...form.register("source")} disabled={locked} />
        </Field>
        <Field label="Qualification">
          <FormSelect control={form.control} name="qualification_status" allowEmpty emptyLabel="Any" disabled={locked}>
            <SelectItem value="unknown">Not yet qualified</SelectItem>
            <SelectItem value="qualified">Qualified</SelectItem>
            <SelectItem value="unqualified">Not qualified</SelectItem>
          </FormSelect>
        </Field>
      </div>
      <div className="rounded-md border border-line bg-surface-2/50 px-3 py-2.5 text-[13px]">
        {!any ? (
          <span className="text-muted">Choose at least one filter. Leads marked do-not-call or with revoked consent are always left out.</span>
        ) : matched.data && callable.data ? (
          <span>
            <span className="font-mono font-medium text-ink">{formatNumber(matched.data.total)}</span> {matched.data.total === 1 ? "lead matches" : "leads match"} ·{" "}
            <span className="font-mono font-medium text-ink">{formatNumber(callable.data.total)}</span> callable
            <span className="text-muted"> — do-not-call and revoked consent are excluded automatically.</span>
          </span>
        ) : (
          <Skeleton className="h-4 w-56" />
        )}
      </div>
    </>
  );
}

function PacingStep({ form }: { form: FormApi }) {
  const e = form.formState.errors;
  const retryOn = form.watch("retry_on");
  const attempts = Number(form.watch("max_attempts")) || 0;
  const backoff = Number(form.watch("backoff_minutes")) || 0;
  const schedule = Array.from({ length: Math.max(0, Math.min(attempts, 5) - 1) }, (_, i) => `attempt ${i + 2} after ${backoff * (i + 1)} min`).join(", ");
  return (
    <>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Concurrent calls" required hint="1–50 at once" error={e.max_concurrency?.message}>
          <Input inputMode="numeric" className="font-mono" {...form.register("max_concurrency")} />
        </Field>
        <Field label="Calls per minute" required hint="1–60 started per minute" error={e.calls_per_minute?.message}>
          <Input inputMode="numeric" className="font-mono" {...form.register("calls_per_minute")} />
        </Field>
        <Field label="Max calls" hint="Stop after this many; empty = no limit" error={e.max_calls?.message}>
          <Input inputMode="numeric" className="font-mono" placeholder="No limit" {...form.register("max_calls")} />
        </Field>
      </div>
      <div className="rounded-md border border-line p-3">
        <p className="text-[13px] font-semibold text-ink">Retries</p>
        <div className="mt-3 grid gap-4 sm:grid-cols-2">
          <Field label="Attempts per lead" required hint="1–5 in total, including the first" error={e.max_attempts?.message}>
            <Input inputMode="numeric" className="font-mono" {...form.register("max_attempts")} />
          </Field>
          <Field label="Backoff (minutes)" required hint={schedule ? `Retry ${schedule}.` : "No retries with a single attempt."} error={e.backoff_minutes?.message}>
            <Input inputMode="numeric" className="font-mono" {...form.register("backoff_minutes")} />
          </Field>
        </div>
        <fieldset className="mt-3">
          <legend className="text-[13px] font-medium text-ink-2">Retry when</legend>
          <div className="mt-1.5 flex flex-wrap gap-4">
            {RETRY_TRIGGERS.map((t) => (
              <label key={t.id} className="flex items-start gap-2 text-[13px]">
                <Checkbox
                  aria-label={t.label}
                  checked={retryOn.includes(t.id)}
                  onCheckedChange={(v) => form.setValue("retry_on", v ? [...retryOn, t.id] : retryOn.filter((x) => x !== t.id), { shouldDirty: true })}
                  className="mt-0.5"
                />
                <span>
                  {t.label}
                  <span className="block text-[12px] text-muted">{t.hint}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>
      </div>
    </>
  );
}

function ReviewStep({ draft, values }: { draft: Campaign; values: WizardInput }) {
  const o = useOrgId();
  const stages = useStages();
  const members = useMembers();
  const agents = useAgents(true);
  const preview = useQuery({
    queryKey: qk.campaignPreview(o, draft.id, draft.updated_at),
    queryFn: () => unwrap(api.POST("/api/v1/campaigns/{campaign_id}/preview", { params: { path: { campaign_id: draft.id } } })),
    enabled: !!o,
  });
  const agent = agents.data?.find((a) => a.id === draft.agent_id);
  const f = draft.lead_filter;
  const chips = [
    f?.stage_id ? `Stage: ${stages.data?.find((s) => s.id === f.stage_id)?.name ?? "…"}` : null,
    f?.unassigned ? "Unassigned" : f?.owner_user_id ? `Owner: ${members.data?.find((m) => m.user.id === f.owner_user_id)?.user.full_name ?? "…"}` : null,
    f?.tag ? `Tag: ${f.tag}` : null,
    f?.source ? `Source: ${f.source}` : null,
    f?.qualification_status ? `Qualification: ${f.qualification_status}` : null,
  ].filter((c): c is string => Boolean(c));
  const skipped = Object.entries(preview.data?.skipped ?? {}).filter(([, n]) => n > 0);

  return (
    <>
      <div className="rounded-md border border-line">
        <div className="border-b border-line px-3 py-2 text-[12.5px] font-semibold text-ink">Who will be called</div>
        {preview.isError ? (
          <p className="px-3 py-3 text-[13px] text-danger">{errorMessage(preview.error)}</p>
        ) : !preview.data ? (
          <div className="flex flex-col gap-2 p-3">
            <Skeleton className="h-4 w-48" />
            <Skeleton className="h-4 w-64" />
          </div>
        ) : (
          <dl className="grid grid-cols-2 gap-x-6 gap-y-2 px-3 py-3 text-[13px] sm:grid-cols-4">
            <div>
              <dt className="eyebrow">Match</dt>
              <dd className="mt-1 font-mono text-[20px] font-medium leading-none">{formatNumber(preview.data.matched)}</dd>
            </div>
            <div>
              <dt className="eyebrow">Will be called</dt>
              <dd className="mt-1 font-mono text-[20px] font-medium leading-none text-success">{formatNumber(preview.data.eligible)}</dd>
            </div>
            <div>
              <dt className="eyebrow">Skipped</dt>
              <dd className="mt-1 font-mono text-[20px] font-medium leading-none text-muted">{formatNumber(preview.data.matched - preview.data.eligible)}</dd>
              {skipped.length ? <dd className="mt-1 text-[12px] text-muted">{skipped.map(([r, n]) => `${skipReasonLabel(r)} ${n}`).join(" · ")}</dd> : null}
            </div>
            <div>
              <dt className="eyebrow">Window</dt>
              <dd className="mt-1 text-[13px]">
                {preview.data.calling_window_open ? <span className="text-success">Open now</span> : <span className="text-muted">Opens {formatDateTime(preview.data.next_window_opens_at)}</span>}
              </dd>
              {preview.data.capped_by_max_calls ? <dd className="mt-1 text-[12px] text-warn">Capped by max calls</dd> : null}
            </div>
          </dl>
        )}
      </div>
      <dl className="divide-y divide-line rounded-md border border-line text-[13px]">
        {[
          ["Agent", agent?.name ?? "—"],
          ["Audience", chips.length ? chips.join(" · ") : "—"],
          ["Schedule", `${draft.start_at ? formatDateTime(draft.start_at) : "As soon as started"} → ${draft.end_at ? formatDateTime(draft.end_at) : "until done"} · ${draft.timezone ?? "organization time zone"}`],
          ["Calling hours", summarizeHours(draft.calling_hours)],
          ["Pacing", `${draft.max_concurrency} concurrent · ${draft.calls_per_minute}/min${draft.max_calls ? ` · stop after ${formatNumber(draft.max_calls)} calls` : ""}`],
          ["Retries", `${draft.retry_policy.max_attempts ?? 1} attempts · on ${(draft.retry_policy.retry_on ?? []).map((r) => r.replace("_", " ")).join(", ") || "nothing"} · ${draft.retry_policy.backoff_minutes ?? 0} min backoff`],
        ].map(([k, v]) => (
          <div key={k} className="grid grid-cols-[110px_minmax(0,1fr)] gap-3 px-3 py-2">
            <dt className="text-muted">{k}</dt>
            <dd className="min-w-0 text-ink-2">{v}</dd>
          </div>
        ))}
      </dl>
      {values.name !== draft.name ? <p className="text-[12px] text-warn">Unsaved edits on earlier steps are applied when you press Continue on that step.</p> : null}
    </>
  );
}

/** Mode choice and launch. Live is offered to admins and owners only, behind a typed confirmation. */
function ReviewActions({ draft, onClose, onStarted }: { draft: Campaign; onClose: () => void; onStarted: (c: Campaign) => void }) {
  const canLaunchLive = useHasRole("admin");
  const invalidate = useInvalidate();
  const [mode, setMode] = useState<CampaignMode>(draft.mode);
  const [confirm, setConfirm] = useState("");
  const startable = draft.status === "draft";
  const locked = isLocked(draft.status);

  const start = useMutation({
    mutationFn: async () => {
      if (mode !== draft.mode) await unwrap(api.PATCH("/api/v1/campaigns/{campaign_id}", { params: { path: { campaign_id: draft.id } }, body: { mode } }));
      return unwrap(api.POST("/api/v1/campaigns/{campaign_id}/start", { params: { path: { campaign_id: draft.id } } }));
    },
    onSuccess: (c) => {
      toast.success(c.status === "scheduled" ? `“${c.name}” scheduled` : `“${c.name}” is running`);
      void invalidate("campaigns");
      void invalidate("campaign", c.id);
      onStarted(c);
    },
    onError: (err) => toast.error(errorMessage(err)),
  });

  if (!startable) {
    return (
      <Button variant="primary" onClick={onClose}>
        Done
      </Button>
    );
  }
  const liveBlocked = mode === "live" && confirm.trim().toUpperCase() !== "LIVE";
  return (
    <div className="flex w-full flex-col gap-3 sm:flex-row sm:items-center sm:justify-end">
      <div className="flex items-center gap-2">
        <Segmented<CampaignMode>
          label="Mode"
          size="sm"
          value={mode}
          onChange={setMode}
          options={[
            { value: "dry_run", label: MODE_BADGE.dry_run.label },
            { value: "live", label: "Live", disabled: !canLaunchLive || locked },
          ]}
        />
        {!canLaunchLive ? (
          <span className="text-[12px] text-muted">Live launches need an administrator.</span>
        ) : mode === "live" ? (
          <label className="flex items-center gap-1.5 text-[12px] text-warn">
            <ShieldAlert className="size-3.5 shrink-0" aria-hidden />
            Type LIVE
            <Input aria-label="Type LIVE to confirm" value={confirm} onChange={(ev) => setConfirm(ev.target.value)} className="h-7 w-20 font-mono uppercase" />
          </label>
        ) : (
          <Badge tone="neutral" title={MODE_BADGE.dry_run.description}>
            Nothing leaves the platform
          </Badge>
        )}
      </div>
      <Button variant="primary" disabled={liveBlocked} loading={start.isPending} onClick={() => start.mutate()}>
        <Rocket /> {mode === "live" ? "Start live campaign" : "Start dry run"}
      </Button>
    </div>
  );
}
