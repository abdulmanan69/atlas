import { zodResolver } from "@hookform/resolvers/zod";
import { useMutation, useQuery } from "@tanstack/react-query";
import { ArrowLeft, CircleAlert, History, MoreHorizontal, Plus, Rocket, Save, Trash2, Workflow } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useFieldArray, useForm, type UseFormReturn } from "react-hook-form";
import { Link, useBlocker, useParams } from "react-router";
import { toast } from "sonner";

import { ErrorState } from "@/components/common";
import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/form";
import { MenuContent, MenuItem, MenuRoot, MenuSeparator, MenuTrigger } from "@/components/ui/menu";
import { Badge, Card, CardHeader, Checkbox, Input, Label, Skeleton, Textarea } from "@/components/ui/primitives";
import { FormSelect, Select, SelectItem } from "@/components/ui/select";
import { Tabs, TabsContent, TabsList } from "@/components/ui/tabs";
import { useInvalidate, useMemberNames, useOrgId } from "@/features/queries";
import { normalizeFlow } from "@/features/voice/flow/types";
import { agentFormSchema, fromForm, LANGUAGES, normalizeChoice, toForm, type AgentForm } from "@/features/voice/agent-form";
import { AgentVariablesProvider, availableVariables, useAgentVariables } from "@/features/voice/agent-variables";
import { CallSettingsTab } from "@/features/voice/flow/call-settings-tab";
import type { FocusRequest } from "@/features/voice/flow/canvas";
import { FlowTab } from "@/features/voice/flow/flow-tab";
import { STATUS_BADGE, useCatalog, useConnections, vk, type AgentDetail } from "@/features/voice/queries";
import { ServiceEditor } from "@/features/voice/service-editor";
import { TestCallPanel } from "@/features/voice/test-call-panel";
import { api, ApiError, errorMessage, unwrap } from "@/lib/api";
import { formatDateTime, formatRelative } from "@/lib/format";
import { useHasRole } from "@/lib/session";
import { cn } from "@/lib/utils";

type PublishIssue = { section: string; field: string; message: string; target?: "node" | "path" | "flow"; id?: string };

export default function AgentEditorPage() {
  const { agentId = "" } = useParams();
  const o = useOrgId();
  const agent = useQuery({
    queryKey: vk.agent(o, agentId),
    queryFn: () => unwrap(api.GET("/api/v1/agents/{agent_id}", { params: { path: { agent_id: agentId } } })),
  });
  if (agent.isError)
    return (
      <Card>
        <ErrorState error={agent.error} onRetry={() => void agent.refetch()} />
      </Card>
    );
  if (!agent.data)
    return (
      <div className="flex flex-col gap-4">
        <Skeleton className="h-9 w-72" />
        <div className="grid gap-5 lg:grid-cols-[minmax(0,1fr)_340px]">
          <Skeleton className="h-[520px]" />
          <Skeleton className="h-72" />
        </div>
      </div>
    );
  return <Editor agent={agent.data} />;
}

function Editor({ agent }: { agent: AgentDetail }) {
  const isManager = useHasRole("manager");
  const canEdit = isManager && agent.status !== "archived";
  const invalidate = useInvalidate();
  const [issues, setIssues] = useState<PublishIssue[]>([]);
  const isFlow = agent.config.mode === "flow";
  const [tab, setTab] = useState(isFlow ? "flow" : "script");
  const [confirmArchive, setConfirmArchive] = useState(false);
  const [confirmConvert, setConfirmConvert] = useState(false);
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const form = useForm<AgentForm>({ resolver: zodResolver(agentFormSchema), defaultValues: toForm(agent.config), mode: "onBlur" });
  const dirty = form.formState.isDirty;

  useEffect(() => {
    form.reset(toForm(agent.config));
  }, [agent.config, agent.latest_version, form]);

  const blocker = useBlocker(({ currentLocation, nextLocation }) => dirty && currentLocation.pathname !== nextLocation.pathname);
  useEffect(() => {
    const onUnload = (e: BeforeUnloadEvent) => {
      if (dirty) e.preventDefault();
    };
    window.addEventListener("beforeunload", onUnload);
    return () => window.removeEventListener("beforeunload", onUnload);
  }, [dirty]);

  const saveConfig = async (values: AgentForm) =>
    unwrap(api.PUT("/api/v1/agents/{agent_id}/config", { params: { path: { agent_id: agent.id } }, body: { config: fromForm(values) } }));

  const save = useMutation({
    mutationFn: saveConfig,
    onSuccess: (a) => {
      toast.success(`Saved as version ${a.latest_version}. Publish to use it on calls.`);
      void invalidate("agent", agent.id);
      void invalidate("agents");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const publish = useMutation({
    mutationFn: async (values: AgentForm | null) => {
      if (values) await saveConfig(values);
      return unwrap(api.POST("/api/v1/agents/{agent_id}/publish", { params: { path: { agent_id: agent.id } }, body: {} }));
    },
    onSuccess: (a) => {
      setIssues([]);
      toast.success(`Version ${a.published_version} is live.`);
      void invalidate("agent", agent.id);
      void invalidate("agents");
    },
    onError: (e) => {
      if (e instanceof ApiError && Array.isArray(e.details)) setIssues(e.details as PublishIssue[]);
      else setIssues([{ section: "Agent", field: "", message: errorMessage(e) }]);
      void invalidate("agent", agent.id);
    },
  });

  const convert = useMutation({
    mutationFn: async (values: AgentForm | null) => {
      if (values) await saveConfig(values);
      return unwrap(api.POST("/api/v1/agents/{agent_id}/convert-to-flow", { params: { path: { agent_id: agent.id } } }));
    },
    onSuccess: (a) => {
      setConfirmConvert(false);
      setTab("flow");
      toast.success(`Version ${a.latest_version} uses the flow builder. Nothing changed on calls until you publish.`);
      void invalidate("agent", agent.id);
      void invalidate("agents");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const lifecycle = useMutation({
    mutationFn: (action: "pause" | "resume" | "archive" | "unarchive") => {
      const params = { params: { path: { agent_id: agent.id } } };
      switch (action) {
        case "pause":
          return unwrap(api.POST("/api/v1/agents/{agent_id}/pause", params));
        case "resume":
          return unwrap(api.POST("/api/v1/agents/{agent_id}/resume", params));
        case "archive":
          return unwrap(api.POST("/api/v1/agents/{agent_id}/archive", params));
        case "unarchive":
          return unwrap(api.POST("/api/v1/agents/{agent_id}/unarchive", params));
      }
    },
    onSuccess: (a) => {
      toast.success(`${a.name} is ${STATUS_BADGE[a.status].label.toLowerCase()}`);
      setConfirmArchive(false);
      void invalidate("agent", agent.id);
      void invalidate("agents");
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const status = STATUS_BADGE[agent.status];
  const errors = form.formState.errors;
  const tabErrors = {
    script: Boolean(errors.persona_name || errors.company_name || errors.greeting || errors.instructions || errors.questions || errors.closing || errors.end_condition),
    flow: Boolean(errors.flow || errors.global_rules) || issues.some((i) => i.target === "node" || i.target === "path"),
    outcomes: Boolean(errors.outcomes || errors.capture_fields),
    settings: Boolean(errors.call_settings || errors.variables),
    voice: Boolean(errors.override_llm || errors.override_tts),
  };
  const wide = isFlow && tab === "flow";
  const showIssue = (i: PublishIssue) => {
    if (!i.target || i.target === "flow" || !i.id) return;
    setTab("flow");
    setFocus({ target: i.target, id: i.id, nonce: Date.now() });
  };
  const dot = (show: boolean) => (show ? <span className="size-1.5 rounded-full bg-danger" aria-label="has errors" /> : null);
  const customVariables = form.watch("variables");
  const variables = useMemo(() => availableVariables(customVariables), [customVariables]);
  const manageVariables = () => {
    setTab("settings");
    requestAnimationFrame(() => document.getElementById("call-settings-variables")?.scrollIntoView({ behavior: "smooth", block: "start" }));
  };

  return (
    <>
      <Link to="/agents" className="mb-4 inline-flex items-center gap-1 text-[12.5px] font-medium text-muted hover:text-ink">
        <ArrowLeft className="size-3.5" /> Agents
      </Link>
      <header className="flex flex-wrap items-start justify-between gap-4 pb-5">
        <div className="min-w-0">
          <h1 className="title-display text-[28px]">{agent.name}</h1>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-[12.5px] text-muted">
            <Badge tone={status.tone}>{status.label}</Badge>
            <span className="font-mono">{agent.published_version ? `v${agent.published_version} live` : "Not published yet"}</span>
            {agent.has_unpublished_changes && agent.published_version ? <span className="text-warn">· v{agent.latest_version} not published</span> : null}
            {dirty ? <span className="text-warn">· Unsaved edits</span> : null}
          </div>
        </div>
        {canEdit ? (
          <div className="flex flex-wrap gap-2">
            <Button disabled={!dirty} loading={save.isPending} onClick={form.handleSubmit((v) => save.mutate(v))}>
              <Save /> Save draft
            </Button>
            <Button
              variant="primary"
              disabled={!dirty && !agent.has_unpublished_changes}
              loading={publish.isPending}
              onClick={dirty ? form.handleSubmit((v) => publish.mutate(v)) : () => publish.mutate(null)}
            >
              <Rocket /> {dirty ? "Save and publish" : "Publish"}
            </Button>
            <MenuRoot>
              <MenuTrigger asChild>
                <Button size="icon" aria-label="More actions">
                  <MoreHorizontal />
                </Button>
              </MenuTrigger>
              <MenuContent>
                {agent.status === "active" ? <MenuItem onSelect={() => lifecycle.mutate("pause")}>Pause agent</MenuItem> : null}
                {agent.status === "paused" ? <MenuItem onSelect={() => lifecycle.mutate("resume")}>Activate agent</MenuItem> : null}
                {!isFlow ? (
                  <MenuItem onSelect={() => setConfirmConvert(true)}>
                    <Workflow /> Switch to flow builder
                  </MenuItem>
                ) : null}
                <MenuSeparator />
                <MenuItem tone="danger" onSelect={() => setConfirmArchive(true)}>
                  Archive agent
                </MenuItem>
              </MenuContent>
            </MenuRoot>
          </div>
        ) : agent.status === "archived" && isManager ? (
          <Button onClick={() => lifecycle.mutate("unarchive")}>Restore agent</Button>
        ) : null}
      </header>

      {issues.length ? (
        <Card className="mb-5 border-danger/30 bg-danger-soft/40 px-4 py-3" role="alert">
          <p className="flex items-center gap-2 text-[13px] font-semibold text-danger">
            <CircleAlert className="size-4" /> This version couldn't be published
          </p>
          <ul className="mt-1.5 list-disc pl-9 text-[13px] text-ink-2">
            {issues.map((i, n) => (
              <li key={n}>
                <span className="font-medium">{i.section}</span>
                {i.field ? <span className="font-mono text-[12px] text-muted"> · {i.field}</span> : null}: {i.message}
                {isFlow && i.id && (i.target === "node" || i.target === "path") ? (
                  <Button variant="link" size="sm" className="ml-2 h-auto" onClick={() => showIssue(i)}>
                    Show on canvas
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      <AgentVariablesProvider value={variables}>
      <div className={cn("grid gap-5", wide ? "grid-cols-1" : "lg:grid-cols-[minmax(0,1fr)_340px]")}>
        <Card className="min-w-0 overflow-clip">
          <Tabs value={tab} onValueChange={setTab}>
            <TabsList
              className="px-3 pt-1"
              tabs={[
                isFlow ? { value: "flow", label: "Flow", badge: dot(tabErrors.flow) } : { value: "script", label: "Script", badge: dot(tabErrors.script) },
                { value: "outcomes", label: "Outcomes & data", badge: dot(tabErrors.outcomes) },
                { value: "settings", label: "Call settings", badge: dot(tabErrors.settings) },
                { value: "voice", label: "Voice & model", badge: dot(tabErrors.voice) },
                { value: "history", label: "History" },
              ]}
            />
            {isFlow ? (
              <TabsContent value="flow" className="p-0">
                <FlowTab
                  key={agent.latest_version}
                  agentId={agent.id}
                  version={agent.latest_version}
                  initialFlow={normalizeFlow(agent.config.flow)}
                  form={form}
                  canEdit={canEdit}
                  dirty={dirty}
                  focus={focus}
                />
              </TabsContent>
            ) : null}
            <fieldset disabled={!canEdit} className="min-w-0">
              {!isFlow ? (
                <TabsContent value="script" className="p-5">
                  <ScriptTab form={form} />
                </TabsContent>
              ) : null}
              <TabsContent value="outcomes" className="p-5">
                <OutcomesTab form={form} isFlow={isFlow} />
              </TabsContent>
              <TabsContent value="settings" className="p-5">
                <CallSettingsTab form={form} agentId={agent.id} canEdit={canEdit} />
              </TabsContent>
              <TabsContent value="voice" className="p-5">
                <VoiceTab form={form} />
              </TabsContent>
            </fieldset>
            <TabsContent value="history" className="p-0">
              <HistoryTab agent={agent} canEdit={canEdit} />
            </TabsContent>
          </Tabs>
        </Card>
        <div className={cn("flex flex-col gap-5", wide && "lg:grid lg:grid-cols-2")}>
          <TestCallPanel agentId={agent.id} published={agent.published_version !== null && agent.status !== "archived"} />
          <Card>
            <CardHeader
              title="Variables"
              description="Lead fields fill on every call; agent variables are set under Call settings"
              action={
                <Button size="sm" variant="ghost" onClick={manageVariables}>
                  Manage
                </Button>
              }
            />
            <ul className="flex flex-wrap gap-1.5 px-4 py-3" aria-label="Variables you can use">
              {variables.map((v) => (
                <li key={v.name}>
                  <code
                    title={v.source === "agent" ? `Agent variable: ${v.value ?? ""}` : "Filled from the lead"}
                    className={cn("rounded px-1.5 py-0.5 font-mono text-[11.5px]", v.source === "agent" ? "bg-accent/10 text-accent" : "bg-surface-2 text-ink-2")}
                  >{`{{${v.name}}}`}</code>
                </li>
              ))}
            </ul>
            <p className="border-t border-line px-4 py-3 text-[12px] text-muted">
              Add a fallback for missing values: <code className="font-mono">{"{{first_name | fallback:there}}"}</code>. Custom lead fields work the same way, and win over an agent variable with the same name.
            </p>
          </Card>
        </div>
      </div>
      </AgentVariablesProvider>

      <ConfirmDialog
        open={blocker.state === "blocked"}
        onOpenChange={(o) => !o && blocker.reset?.()}
        title="Leave without saving?"
        description="Your unsaved edits to this agent will be lost."
        confirmLabel="Discard edits"
        onConfirm={() => blocker.proceed?.()}
      />
      <ConfirmDialog
        open={confirmConvert}
        onOpenChange={setConfirmConvert}
        title="Switch to the flow builder?"
        description={
          <>
            Your script becomes a flow that says and does exactly the same: a Start with the greeting and instructions, one End with the wrap-up and captured details, and a
            path between them. From there you can add steps and branches on a canvas. This is one-way: there is no switching back. Live calls keep using the published
            version until you publish again.{dirty ? " Your unsaved edits are saved first." : ""}
          </>
        }
        confirmLabel="Switch to flow builder"
        tone="primary"
        loading={convert.isPending}
        onConfirm={dirty ? form.handleSubmit((v) => convert.mutate(v)) : () => convert.mutate(null)}
      />
      <ConfirmDialog
        open={confirmArchive}
        onOpenChange={setConfirmArchive}
        title={`Archive ${agent.name}?`}
        description="It stops taking calls and is hidden from lists. Its versions and call history are kept, and you can restore it later."
        confirmLabel="Archive agent"
        loading={lifecycle.isPending}
        onConfirm={() => lifecycle.mutate("archive")}
      />
    </>
  );
}

/** Inserts a variable at the cursor of the last focused script field. */
function useVariableInserter(form: UseFormReturn<AgentForm>) {
  const last = useRef<{ name: "greeting" | "instructions" | "closing"; el: HTMLTextAreaElement | HTMLInputElement } | null>(null);
  const track = (name: "greeting" | "instructions" | "closing") => ({
    onFocus: (e: React.FocusEvent<HTMLTextAreaElement | HTMLInputElement>) => {
      last.current = { name, el: e.currentTarget };
    },
  });
  const insert = (variable: string) => {
    const target = last.current ?? { name: "instructions" as const, el: null };
    const current = form.getValues(target.name) ?? "";
    const pos = target.el?.selectionStart ?? current.length;
    const token = `{{${variable}}}`;
    form.setValue(target.name, current.slice(0, pos) + token + current.slice(pos), { shouldDirty: true });
    requestAnimationFrame(() => {
      target.el?.focus();
      target.el?.setSelectionRange(pos + token.length, pos + token.length);
    });
  };
  return { track, insert };
}

function ScriptTab({ form }: { form: UseFormReturn<AgentForm> }) {
  const e = form.formState.errors;
  const variables = useAgentVariables();
  const questions = useFieldArray({ control: form.control, name: "questions" });
  const { track, insert } = useVariableInserter(form);
  const greeting = form.register("greeting");
  const instructions = form.register("instructions");
  const closing = form.register("closing");
  return (
    <div className="flex flex-col gap-6">
      <section className="grid gap-4 sm:grid-cols-3">
        <Field label="Agent name" hint="What it calls itself" error={e.persona_name?.message} required>
          <Input {...form.register("persona_name")} />
        </Field>
        <Field label="Calling for" hint="Company it represents" error={e.company_name?.message} required>
          <Input {...form.register("company_name")} />
        </Field>
        <Field label="Language" error={e.language?.message}>
          <FormSelect control={form.control} name="language">
            {LANGUAGES.map(([code, name]) => (
              <SelectItem key={code} value={code}>
                {name}
              </SelectItem>
            ))}
          </FormSelect>
        </Field>
      </section>

      <div className="flex flex-wrap items-center gap-1.5 rounded-md bg-surface-2/70 px-3 py-2 text-[12px] text-muted">
        Insert variable:
        {variables.map((v) => (
          <button
            key={v.name}
            type="button"
            title={v.source === "agent" ? `Agent variable: ${v.value ?? ""}` : "Filled from the lead"}
            onMouseDown={(ev) => ev.preventDefault()}
            onClick={() => insert(v.name)}
            className={cn(
              "rounded border bg-surface px-1.5 py-0.5 font-mono text-[11.5px] text-ink-2 hover:border-accent hover:text-accent",
              v.source === "agent" ? "border-accent/40" : "border-line",
            )}
          >
            {v.name}
          </button>
        ))}
      </div>

      <Field label="Greeting" hint="The first sentence on every call. Ask permission to continue." error={e.greeting?.message} required>
        <Textarea rows={2} {...greeting} {...track("greeting")} />
      </Field>
      <Field label="Instructions" hint="The goal, what you offer, tone, and anything the agent must or must not say." error={e.instructions?.message} required>
        <Textarea rows={8} className="min-h-40" {...instructions} {...track("instructions")} />
      </Field>

      <section className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <Label>Questions to cover</Label>
          <Button type="button" size="sm" variant="ghost" disabled={questions.fields.length >= 15} onClick={() => questions.append({ value: "" })}>
            <Plus /> Add question
          </Button>
        </div>
        {questions.fields.length === 0 ? <p className="text-[12.5px] text-muted">No fixed questions. The agent follows the instructions above.</p> : null}
        <ol className="flex flex-col gap-2">
          {questions.fields.map((f, i) => (
            <li key={f.id} className="flex items-start gap-2">
              <span className="mt-1.5 w-5 shrink-0 text-right font-mono text-[12px] text-faint">{i + 1}</span>
              <div className="flex-1">
                <Input aria-label={`Question ${i + 1}`} {...form.register(`questions.${i}.value`)} />
                {e.questions?.[i]?.value ? <p className="mt-1 text-[12px] text-danger">{e.questions[i]?.value?.message}</p> : null}
              </div>
              <Button type="button" size="icon-sm" variant="ghost" aria-label={`Remove question ${i + 1}`} onClick={() => questions.remove(i)}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ol>
      </section>

      <section className="grid gap-4 sm:grid-cols-2">
        <Field label="When to wrap up" error={e.end_condition?.message}>
          <Textarea rows={3} {...form.register("end_condition")} />
        </Field>
        <Field label="How to end the call" error={e.closing?.message}>
          <Textarea rows={3} {...closing} {...track("closing")} />
        </Field>
      </section>

      <section className="flex flex-col gap-3 rounded-lg border border-line px-4 py-3">
        <label className="flex items-start gap-2.5">
          <Checkbox aria-label="Let callers interrupt" checked={form.watch("allow_interrupt")} onCheckedChange={(v) => form.setValue("allow_interrupt", v, { shouldDirty: true })} className="mt-0.5" />
          <span>
            <span className="block text-[13px] font-medium">Let people interrupt the agent</span>
            <span className="block text-[12px] text-muted">The agent stops speaking when the person talks over it. Feels more natural on most calls.</span>
          </span>
        </label>
        <label className="flex items-start gap-2.5">
          <Checkbox aria-label="AI disclosure" checked={form.watch("ai_disclosure")} onCheckedChange={(v) => form.setValue("ai_disclosure", v, { shouldDirty: true })} className="mt-0.5" />
          <span>
            <span className="block text-[13px] font-medium">Confirm it's an AI when asked</span>
            <span className="block text-[12px] text-muted">Required by law in many places. Keep this on unless your counsel says otherwise.</span>
          </span>
        </label>
      </section>
    </div>
  );
}

function OutcomesTab({ form, isFlow }: { form: UseFormReturn<AgentForm>; isFlow: boolean }) {
  const e = form.formState.errors;
  const outcomes = useFieldArray({ control: form.control, name: "outcomes" });
  const fields = useFieldArray({ control: form.control, name: "capture_fields" });
  return (
    <div className="flex flex-col gap-8">
      <section>
        <div className="flex items-end justify-between gap-4">
          <div>
            <h3 className="text-[13.5px] font-semibold">Call outcomes</h3>
            <p className="mt-0.5 text-[12.5px] text-muted">At the end of each call, the agent picks the outcome that fits best. Outcomes drive stage changes and follow-ups.</p>
          </div>
          <Button type="button" size="sm" variant="ghost" onClick={() => outcomes.append({ code: "", description: "" })} disabled={outcomes.fields.length >= 20}>
            <Plus /> Add outcome
          </Button>
        </div>
        <ul className="mt-3 divide-y divide-line rounded-lg border border-line">
          {outcomes.fields.map((f, i) => (
            <li key={f.id} className="grid grid-cols-[180px_minmax(0,1fr)_auto] items-start gap-2 px-3 py-2.5">
              <div>
                <Input aria-label="Outcome code" className="font-mono" {...form.register(`outcomes.${i}.code`)} />
                {e.outcomes?.[i]?.code ? <p className="mt-1 text-[11.5px] text-danger">{e.outcomes[i]?.code?.message}</p> : null}
              </div>
              <div>
                <Input aria-label="Outcome description" {...form.register(`outcomes.${i}.description`)} />
                {e.outcomes?.[i]?.description ? <p className="mt-1 text-[11.5px] text-danger">{e.outcomes[i]?.description?.message}</p> : null}
              </div>
              <Button type="button" size="icon-sm" variant="ghost" aria-label="Remove outcome" onClick={() => outcomes.remove(i)}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      </section>

      {isFlow ? (
        <section className="rounded-lg border border-line px-4 py-3">
          <h3 className="text-[13.5px] font-semibold">Details to capture</h3>
          <p className="mt-0.5 text-[12.5px] text-muted">
            In the flow builder, details are captured per Step and End: select one on the Flow tab. Whether the person opted out is always captured, so do-not-call
            requests are honoured automatically.
          </p>
        </section>
      ) : (
      <section>
        <div className="flex items-end justify-between gap-4">
          <div>
            <h3 className="text-[13.5px] font-semibold">Details to capture</h3>
            <p className="mt-0.5 text-[12.5px] text-muted">
              Saved on the call record and the lead's timeline. Whether the person opted out is always captured, so do-not-call requests are honoured
              automatically.
            </p>
          </div>
          <Button type="button" size="sm" variant="ghost" onClick={() => fields.append({ name: "", type: "string", description: "" })} disabled={fields.fields.length >= 20}>
            <Plus /> Add field
          </Button>
        </div>
        {fields.fields.length === 0 ? <p className="mt-3 text-[12.5px] text-muted">No extra fields yet.</p> : null}
        <ul className="mt-3 flex flex-col gap-2">
          {fields.fields.map((f, i) => (
            <li key={f.id} className="grid grid-cols-[160px_120px_minmax(0,1fr)_auto] items-start gap-2">
              <div>
                <Input aria-label="Field name" placeholder="budget" className="font-mono" {...form.register(`capture_fields.${i}.name`)} />
                {e.capture_fields?.[i]?.name ? <p className="mt-1 text-[11.5px] text-danger">{e.capture_fields[i]?.name?.message}</p> : null}
              </div>
              <FormSelect aria-label="Field type" control={form.control} name={`capture_fields.${i}.type`}>
                <SelectItem value="string">Text</SelectItem>
                <SelectItem value="number">Number</SelectItem>
                <SelectItem value="boolean">Yes / no</SelectItem>
              </FormSelect>
              <div>
                <Input aria-label="What to capture" placeholder="Budget they mentioned, with currency" {...form.register(`capture_fields.${i}.description`)} />
                {e.capture_fields?.[i]?.description ? <p className="mt-1 text-[11.5px] text-danger">{e.capture_fields[i]?.description?.message}</p> : null}
              </div>
              <Button type="button" size="icon-sm" variant="ghost" aria-label="Remove field" onClick={() => fields.remove(i)}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      </section>
      )}
    </div>
  );
}

function VoiceTab({ form }: { form: UseFormReturn<AgentForm> }) {
  const catalog = useCatalog();
  const connections = useConnections();
  const llm = form.watch("override_llm");
  const tts = form.watch("override_tts");
  const row = (kind: "llm" | "tts", title: string, description: string, value: typeof llm) => {
    const key = kind === "llm" ? "override_llm" : "override_tts";
    return (
      <section className="rounded-lg border border-line">
        <div className="flex items-start justify-between gap-4 px-4 py-3">
          <div>
            <h3 className="text-[13.5px] font-semibold">{title}</h3>
            <p className="mt-0.5 text-[12.5px] text-muted">{description}</p>
          </div>
          <Select
            aria-label={`${title} source`}
            className="w-56"
            value={value ? "custom" : "default"}
            onValueChange={(v) => form.setValue(key, v === "custom" ? { connection_id: "", settings: {} } : null, { shouldDirty: true })}
          >
            <SelectItem value="default">Organization default</SelectItem>
            <SelectItem value="custom">Custom for this agent</SelectItem>
          </Select>
        </div>
        {value ? (
          <div className="border-t border-line px-4 py-4">
            <ServiceEditor
              kind={kind}
              value={value.connection_id ? value : null}
              onChange={(v) => form.setValue(key, normalizeChoice(v) ?? { connection_id: "", settings: {} }, { shouldDirty: true })}
              catalog={catalog.data}
              connections={connections.data}
            />
            {form.formState.errors[key] ? <p className="mt-2 text-[12px] text-danger">Choose a connection</p> : null}
          </div>
        ) : null}
      </section>
    );
  };
  return (
    <div className="flex flex-col gap-4">
      <p className="text-[13px] text-muted">
        Agents use the organization's models by default (Settings → <Link to="/ai-providers" className="text-accent hover:underline">AI providers</Link>). Override them here, for example to give this agent a different voice.
      </p>
      {row("tts", "Voice", "How this agent sounds", tts)}
      {row("llm", "Language model", "Use a stronger or cheaper model for this agent", llm)}
    </div>
  );
}

function HistoryTab({ agent, canEdit }: { agent: AgentDetail; canEdit: boolean }) {
  const o = useOrgId();
  const names = useMemberNames();
  const invalidate = useInvalidate();
  const versions = useQuery({
    queryKey: vk.versions(o, agent.id),
    queryFn: () => unwrap(api.GET("/api/v1/agents/{agent_id}/versions", { params: { path: { agent_id: agent.id } } })),
  });
  const restore = useMutation({
    mutationFn: (v: number) => unwrap(api.POST("/api/v1/agents/{agent_id}/versions/{version}/restore", { params: { path: { agent_id: agent.id, version: v } } })),
    onSuccess: (a) => {
      toast.success(`Restored as version ${a.latest_version}. Publish it to go live.`);
      void invalidate("agent", agent.id);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });
  return (
    <ol className="divide-y divide-line">
      {(versions.data ?? []).map((v) => (
        <li key={v.version} className="flex items-center gap-3 px-5 py-3">
          <span className={cn("grid size-7 shrink-0 place-items-center rounded-full border font-mono text-[11px]", v.is_published ? "border-success bg-success-soft text-success" : "border-line text-muted")}>
            {v.version}
          </span>
          <div className="min-w-0 flex-1">
            <p className="flex items-center gap-2 text-[13px] font-medium">
              {v.note ?? `Version ${v.version}`} {v.is_published ? <Badge tone="success">Live</Badge> : null}
            </p>
            <p className="text-[12px] text-muted" title={formatDateTime(v.created_at)}>
              {(v.created_by_user_id && names.get(v.created_by_user_id)) || "Someone"} · {formatRelative(v.created_at)}
            </p>
          </div>
          {canEdit && v.version !== agent.latest_version ? (
            <Button size="sm" variant="ghost" loading={restore.isPending && restore.variables === v.version} onClick={() => restore.mutate(v.version)}>
              <History /> Restore
            </Button>
          ) : null}
        </li>
      ))}
      {versions.isLoading ? <li className="p-5"><Skeleton className="h-10" /></li> : null}
    </ol>
  );
}
