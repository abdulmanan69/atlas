import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation } from "@tanstack/react-query";
import { KeyRound, Plus, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, type ReactNode } from "react";
import { Controller, useFieldArray, useForm, type UseFormReturn } from "react-hook-form";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Badge, Input, Label, Textarea } from "@/components/ui/primitives";
import { Segmented } from "@/components/ui/segmented";
import { FormSelect, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import {
  actionSchema,
  AUTH_LABEL,
  AUTH_TYPES,
  deriveFunctionName,
  EMPTY_AUTH,
  emptyActionValues,
  MAX_HEADERS,
  MAX_PARAMS,
  METHODS,
  PARAM_TYPES,
  toCreateBody,
  toUpdateBody,
  valuesFromAction,
  type Action,
  type ActionFormValues,
} from "@/features/actions/action-schema";
import { useInvalidate } from "@/features/queries";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { cn } from "@/lib/utils";

type Form = UseFormReturn<ActionFormValues>;

export function ActionEditorDialog({ open, onOpenChange, existing }: { open: boolean; onOpenChange: (open: boolean) => void; existing?: Action }) {
  const invalidate = useInvalidate();
  const form = useForm<ActionFormValues>({
    resolver: zodResolver(actionSchema),
    defaultValues: existing ? valuesFromAction(existing) : emptyActionValues(),
    mode: "onBlur",
  });
  const { register, control, formState, watch, setError, reset } = form;
  const errors = formState.errors;

  useEffect(() => {
    if (open) reset(existing ? valuesFromAction(existing) : emptyActionValues());
  }, [open, existing, reset]);

  const save = useMutation({
    mutationFn: (v: ActionFormValues) =>
      existing
        ? unwrap(api.PATCH("/api/v1/actions/{action_id}", { params: { path: { action_id: existing.id } }, body: toUpdateBody(v, existing) }))
        : unwrap(api.POST("/api/v1/actions", { body: toCreateBody(v) })),
    onSuccess: (a) => {
      toast.success(existing ? `Saved ${a.name}` : `Created ${a.name}. Attach it to a step on an agent's flow.`);
      void invalidate("actions");
      onOpenChange(false);
    },
    onError: (e) => {
      // The destination policy reports which field it refused; put the server's words on that field too.
      if (e instanceof ApiError && typeof e.details === "object" && e.details && (e.details as { field?: string }).field === "url") {
        setError("url", { message: e.message });
      }
    },
  });

  const name = watch("name");
  const method = watch("method");
  const hasBody = method === "POST" || method === "PUT" || method === "PATCH";
  const fn = deriveFunctionName(name ?? "");
  const submit = form.handleSubmit((v) => save.mutate(v));

  return (
    <Dialog
      open={open}
      onOpenChange={(o) => {
        onOpenChange(o);
        if (!o) save.reset();
      }}
      title={existing ? `Edit ${existing.name}` : "New action"}
      description="An HTTP call the agent can make during a conversation. The model decides when to use it from the name and description, and fills in the parameters."
      width="max-w-2xl"
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" loading={save.isPending} onClick={() => void submit()}>
            {existing ? "Save action" : "Create action"}
          </Button>
        </>
      }
    >
      <form className="flex flex-col gap-6" noValidate onSubmit={submit}>
        <FormError message={save.error ? errorMessage(save.error) : null} />

        <Section title="What the model sees" description="The model reads these to decide when to call the action.">
          <Field
            label="Name"
            required
            error={errors.name?.message}
            hint={
              fn ? (
                <>
                  Called as <code className="font-mono text-ink-2">{fn}</code>. Must be unique among the actions on a step.
                </>
              ) : (
                "Short and specific, for example “Book a viewing”."
              )
            }
          >
            <Input {...register("name")} maxLength={80} autoFocus={!existing} autoComplete="off" />
          </Field>
          <Field label="Description" required error={errors.description?.message} hint="Write it as an instruction: when to use it, and what it returns.">
            <Textarea {...register("description")} rows={3} maxLength={2000} placeholder="Books a property viewing once the caller has confirmed a date and time. Returns the booking reference." />
          </Field>
        </Section>

        <Section title="Request">
          <div className="grid gap-4 sm:grid-cols-[132px_minmax(0,1fr)]">
            <Field label="Method" error={errors.method?.message}>
              <FormSelect control={control} name="method">
                {METHODS.map((m) => (
                  <SelectItem key={m} value={m} className="font-mono">
                    {m}
                  </SelectItem>
                ))}
              </FormSelect>
            </Field>
            <Field label="URL" required error={errors.url?.message} hint="https:// only. Private, loopback and internal addresses are refused; the host is checked again before every call.">
              <Input {...register("url")} className="font-mono" placeholder="https://api.example.com/viewings" spellCheck={false} autoComplete="off" />
            </Field>
          </div>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Timeout" error={errors.timeout_ms?.message} hint="100 to 15000 ms. The agent keeps talking while it waits.">
              <Input {...register("timeout_ms", { valueAsNumber: true })} inputMode="numeric" className="font-mono" />
            </Field>
            {hasBody ? (
              <Controller
                control={control}
                name="body_format"
                render={({ field }) => (
                  <div className="flex flex-col gap-1.5">
                    <Label>Body format</Label>
                    <Segmented
                      label="Body format"
                      value={field.value}
                      onChange={field.onChange}
                      options={[
                        { value: "json", label: "JSON" },
                        { value: "form", label: "Form fields" },
                      ]}
                    />
                    <span className="text-[12px] text-muted">Parameters are sent as one flat object, presets first.</span>
                  </div>
                )}
              />
            ) : (
              <p className="self-end pb-1 text-[12px] text-muted">
                <span className="font-mono">{method}</span> sends parameters as the query string.
              </p>
            )}
          </div>
          <HeaderRows form={form} />
        </Section>

        <Section title="Parameters the model fills in" description="The model supplies these from the conversation. Describe each so it knows what to ask for.">
          <ParameterRows form={form} />
        </Section>

        <Section
          title="Values taken from the call"
          description={
            <>
              Filled in automatically, never asked of the model. Use <code className="font-mono">{"{{initial_context.phone_number}}"}</code> for call-start context such as lead fields, or{" "}
              <code className="font-mono">{"{{gathered_context.x}}"}</code> for values captured earlier in the call.
            </>
          }
        >
          <PresetRows form={form} />
        </Section>

        <Section title="Authentication" description="Sent with every request. Stored by the voice service as a credential and never shown again.">
          <AuthEditor form={form} existing={existing} />
        </Section>

        <Section title="While it runs">
          <Field label="What the agent says" error={errors.say_while_running?.message} hint="A short sentence spoken while the request is in flight. Leave empty for silence.">
            <Input {...register("say_while_running")} maxLength={300} placeholder="Let me check that for you." />
          </Field>
        </Section>
        <button type="submit" hidden />
      </form>
    </Dialog>
  );
}

function Section({ title, description, children }: { title: string; description?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <div>
        <h3 className="text-[13px] font-semibold text-ink">{title}</h3>
        {description ? <p className="mt-0.5 text-[12px] text-muted">{description}</p> : null}
      </div>
      {children}
    </section>
  );
}

function RowShell({ children, onRemove, label }: { children: ReactNode; onRemove: () => void; label: string }) {
  return (
    <div className="flex items-start gap-2 rounded-md border border-line bg-surface-2/40 p-2">
      <div className="grid min-w-0 flex-1 gap-2">{children}</div>
      <Button size="icon-sm" variant="ghost" aria-label={label} onClick={onRemove} className="mt-0.5 text-muted hover:text-danger">
        <Trash2 />
      </Button>
    </div>
  );
}

function HeaderRows({ form }: { form: Form }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "headers" });
  const errors = form.formState.errors.headers;
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <Label>Static headers</Label>
        <Button size="sm" variant="ghost" disabled={fields.length >= MAX_HEADERS} onClick={() => append({ name: "", value: "" })}>
          <Plus /> Add header
        </Button>
      </div>
      {fields.length === 0 ? <p className="text-[12px] text-muted">None. Sent with every request; authentication headers belong in the section below.</p> : null}
      {fields.map((f, i) => (
        <RowShell key={f.id} onRemove={() => remove(i)} label={`Remove header ${i + 1}`}>
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
            <Field label="Header" error={errors?.[i]?.name?.message} className="gap-1">
              <Input {...form.register(`headers.${i}.name`)} className="font-mono" placeholder="X-Source" spellCheck={false} />
            </Field>
            <Field label="Value" error={errors?.[i]?.value?.message} className="gap-1">
              <Input {...form.register(`headers.${i}.value`)} className="font-mono" placeholder="atlas" spellCheck={false} />
            </Field>
          </div>
        </RowShell>
      ))}
    </div>
  );
}

function TypeSelect({ form, name }: { form: Form; name: `parameters.${number}.type` | `preset_parameters.${number}.type` }) {
  return (
    <FormSelect control={form.control} name={name} aria-label="Type">
      {PARAM_TYPES.map((t) => (
        <SelectItem key={t} value={t} className="font-mono">
          {t}
        </SelectItem>
      ))}
    </FormSelect>
  );
}

function RequiredSwitch({ form, name, label }: { form: Form; name: `parameters.${number}.required` | `preset_parameters.${number}.required`; label: string }) {
  return (
    <Controller
      control={form.control}
      name={name}
      render={({ field }) => (
        <label className="flex h-8 items-center gap-2 self-end text-[12.5px] text-ink-2">
          <Switch size="sm" checked={field.value} onCheckedChange={field.onChange} aria-label={label} />
          Required
        </label>
      )}
    />
  );
}

function ParameterRows({ form }: { form: Form }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "parameters" });
  const errors = form.formState.errors.parameters;
  return (
    <div className="flex flex-col gap-2">
      {fields.length === 0 ? <p className="text-[12px] text-muted">No parameters. The action is called with only the preset values.</p> : null}
      {fields.map((f, i) => (
        <RowShell key={f.id} onRemove={() => remove(i)} label={`Remove parameter ${i + 1}`}>
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_120px_auto]">
            <Field label="Name" error={errors?.[i]?.name?.message} className="gap-1">
              <Input {...form.register(`parameters.${i}.name`)} className="font-mono" placeholder="date" spellCheck={false} />
            </Field>
            <Field label="Type" className="gap-1">
              <TypeSelect form={form} name={`parameters.${i}.type`} />
            </Field>
            <RequiredSwitch form={form} name={`parameters.${i}.required`} label={`Parameter ${i + 1} required`} />
          </div>
          <Field label="Description" error={errors?.[i]?.description?.message} className="gap-1">
            <Input {...form.register(`parameters.${i}.description`)} placeholder="The viewing date the caller asked for, as YYYY-MM-DD" />
          </Field>
        </RowShell>
      ))}
      <Button size="sm" variant="secondary" className="self-start" disabled={fields.length >= MAX_PARAMS} onClick={() => append({ name: "", type: "string", description: "", required: true })}>
        <Plus /> Add parameter
      </Button>
    </div>
  );
}

function PresetRows({ form }: { form: Form }) {
  const { fields, append, remove } = useFieldArray({ control: form.control, name: "preset_parameters" });
  const errors = form.formState.errors.preset_parameters;
  return (
    <div className="flex flex-col gap-2">
      {fields.map((f, i) => (
        <RowShell key={f.id} onRemove={() => remove(i)} label={`Remove preset value ${i + 1}`}>
          <div className="grid gap-2 sm:grid-cols-[minmax(0,1fr)_120px_auto]">
            <Field label="Name" error={errors?.[i]?.name?.message} className="gap-1">
              <Input {...form.register(`preset_parameters.${i}.name`)} className="font-mono" placeholder="phone" spellCheck={false} />
            </Field>
            <Field label="Type" className="gap-1">
              <TypeSelect form={form} name={`preset_parameters.${i}.type`} />
            </Field>
            <RequiredSwitch form={form} name={`preset_parameters.${i}.required`} label={`Preset value ${i + 1} required`} />
          </div>
          <Field label="Value or template" error={errors?.[i]?.value?.message} className="gap-1">
            <Input {...form.register(`preset_parameters.${i}.value`)} className="font-mono" placeholder="{{initial_context.phone_number}}" spellCheck={false} />
          </Field>
        </RowShell>
      ))}
      <Button size="sm" variant="secondary" className="self-start" disabled={fields.length >= MAX_PARAMS} onClick={() => append({ name: "", type: "string", value: "", required: true })}>
        <Plus /> Add preset value
      </Button>
    </div>
  );
}

function AuthEditor({ form, existing }: { form: Form; existing: Action | undefined }) {
  const mode = form.watch("auth_mode");
  const type = form.watch("auth.type");
  const errors = form.formState.errors.auth;
  const stored = existing?.auth ?? null;
  const setMode = (m: ActionFormValues["auth_mode"]) => {
    form.setValue("auth_mode", m, { shouldDirty: true });
    if (m === "set") form.setValue("auth", { ...EMPTY_AUTH, type: stored?.type ?? "bearer", header_name: stored?.header_name ?? "" });
  };

  if (mode === "keep" && stored) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-line px-3 py-2.5">
        <span className="flex items-center gap-2 text-[13px]">
          <KeyRound className="size-4 text-muted" aria-hidden />
          <span className="font-medium">{AUTH_LABEL[stored.type]}</span>
          {stored.header_name ? <code className="font-mono text-[12px] text-muted">{stored.header_name}</code> : null}
          <Badge tone="success">Configured</Badge>
        </span>
        <span className="flex gap-1">
          <Button size="sm" onClick={() => setMode("set")}>
            Replace
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setMode("none")}>
            Remove
          </Button>
        </span>
      </div>
    );
  }
  if (mode === "none") {
    return (
      <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-dashed border-line-strong px-3 py-2.5 text-[13px] text-muted">
        <span>{stored ? "The stored credential will be removed when you save." : "No credential. The request is sent as-is."}</span>
        <span className="flex gap-1">
          {stored ? (
            <Button size="sm" variant="ghost" onClick={() => setMode("keep")}>
              Keep stored
            </Button>
          ) : null}
          <Button size="sm" onClick={() => setMode("set")}>
            Add credential
          </Button>
        </span>
      </div>
    );
  }
  const secret = { type: "password" as const, autoComplete: "new-password", spellCheck: false, className: "font-mono" };
  return (
    <div className="flex flex-col gap-3 rounded-md border border-line p-3">
      <div className="grid gap-3 sm:grid-cols-[200px_minmax(0,1fr)]">
        <Field label="Type" hint={AUTH_TYPES.find((a) => a.value === type)?.description}>
          <FormSelect control={form.control} name="auth.type">
            {AUTH_TYPES.map((a) => (
              <SelectItem key={a.value} value={a.value} description={a.description}>
                {a.label}
              </SelectItem>
            ))}
          </FormSelect>
        </Field>
        <div className={cn("grid gap-3", type === "basic" && "sm:grid-cols-2")}>
          {type === "bearer" ? (
            <Field label="Token" required error={errors?.token?.message}>
              <Input {...form.register("auth.token")} {...secret} />
            </Field>
          ) : null}
          {type === "api_key" ? (
            <>
              <Field label="Header name" required error={errors?.header_name?.message}>
                <Input {...form.register("auth.header_name")} className="font-mono" placeholder="X-API-Key" spellCheck={false} />
              </Field>
              <Field label="Key" required error={errors?.api_key?.message}>
                <Input {...form.register("auth.api_key")} {...secret} />
              </Field>
            </>
          ) : null}
          {type === "basic" ? (
            <>
              <Field label="Username" required error={errors?.username?.message}>
                <Input {...form.register("auth.username")} autoComplete="off" className="font-mono" />
              </Field>
              <Field label="Password" required error={errors?.password?.message}>
                <Input {...form.register("auth.password")} {...secret} />
              </Field>
            </>
          ) : null}
          {type === "custom_header" ? (
            <>
              <Field label="Header name" required error={errors?.header_name?.message}>
                <Input {...form.register("auth.header_name")} className="font-mono" placeholder="X-Auth" spellCheck={false} />
              </Field>
              <Field label="Header value" required error={errors?.header_value?.message}>
                <Input {...form.register("auth.header_value")} {...secret} />
              </Field>
            </>
          ) : null}
        </div>
      </div>
      <div className="flex items-center justify-between gap-3">
        <p className="flex items-start gap-1.5 text-[12px] text-muted">
          <ShieldCheck className="mt-px size-3.5 shrink-0 text-success" aria-hidden />
          Encrypted by the voice service; ATLAS keeps only the type and header name.
        </p>
        <Button size="sm" variant="ghost" onClick={() => setMode(stored ? "keep" : "none")}>
          {stored ? "Keep stored instead" : "No credential"}
        </Button>
      </div>
    </div>
  );
}
