/**
 * Right-hand inspector: edits the selected node or path. Each form validates with zod and
 * commits every valid change to the editor reducer, so the canvas updates as you type and
 * undo works across the inspector too.
 */
import { zodResolver } from "@hookform/resolvers/zod";
import { ArrowRight, ChevronDown, CircleAlert, Eye, Plus, Trash2, TriangleAlert } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useFieldArray, useForm, type UseFormReturn } from "react-hook-form";

import { Button } from "@/components/ui/button";
import { Field } from "@/components/ui/form";
import { Input, Kbd, Textarea } from "@/components/ui/primitives";
import { FormSelect, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { VARIABLES } from "@/features/voice/agent-form";
import { nodeFormSchema, pathFormSchema, type NodeForm, type PathForm } from "@/features/voice/flow/inspector-schema";
import type { EditorAction, EditorState } from "@/features/voice/flow/model";
import { ActionsPicker, KnowledgePicker } from "@/features/voice/flow/pickers";
import { useCompiledPrompt } from "@/features/voice/flow/queries";
import { KIND_LABEL, LIMITS, type FlowIssue, type FlowNode, type FlowPath } from "@/features/voice/flow/types";
import { pathFunctionName, targetKey } from "@/features/voice/flow/validate";
import { cn } from "@/lib/utils";

export interface InspectorProps {
  state: EditorState;
  dispatch: (action: EditorAction) => void;
  errors: Map<string, FlowIssue[]>;
  warnings: Map<string, FlowIssue[]>;
  readOnly: boolean;
  globalRules: string;
  onEditGlobalRules: () => void;
  agentId: string;
  version: number;
  /** Unsaved edits exist: the saved-version preview would be stale. */
  dirty: boolean;
}

export function Inspector(props: InspectorProps) {
  const { state, errors, warnings } = props;
  const sel = state.selection;
  if (sel?.kind === "node") {
    const node = state.flow.nodes.find((n) => n.id === sel.id);
    if (node) {
      const key = targetKey("node", node.id);
      return <NodeInspector key={node.id} node={node} issues={errors.get(key) ?? []} warningIssues={warnings.get(key) ?? []} {...props} />;
    }
  }
  if (sel?.kind === "path") {
    const path = state.flow.paths.find((p) => p.id === sel.id);
    if (path) {
      const key = targetKey("path", path.id);
      return <PathInspector key={path.id} path={path} issues={errors.get(key) ?? []} warningIssues={warnings.get(key) ?? []} {...props} />;
    }
  }
  return <FlowSummary {...props} />;
}

// ------------------------------------------------------------------ shared pieces

function Section({ title, children, hint }: { title: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-3">
      <div>
        <h3 className="text-[12.5px] font-semibold text-ink">{title}</h3>
        {hint ? <p className="mt-0.5 text-[12px] text-muted">{hint}</p> : null}
      </div>
      {children}
    </section>
  );
}

function IssueList({ issues, warningIssues }: { issues: FlowIssue[]; warningIssues: FlowIssue[] }) {
  if (!issues.length && !warningIssues.length) return null;
  return (
    <ul className="flex flex-col gap-1" aria-label="Problems with this item">
      {issues.map((i, n) => (
        <li key={`e${n}`} className="flex items-start gap-1.5 rounded-md bg-danger-soft px-2.5 py-1.5 text-[12px] text-danger">
          <CircleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden /> {i.message}
        </li>
      ))}
      {warningIssues.map((i, n) => (
        <li key={`w${n}`} className="flex items-start gap-1.5 rounded-md bg-warn-soft px-2.5 py-1.5 text-[12px] text-warn">
          <TriangleAlert className="mt-0.5 size-3.5 shrink-0" aria-hidden /> {i.message}
        </li>
      ))}
    </ul>
  );
}

/** Inserts `{{variable}}` at the caret of the text field that was focused last. */
function useVariableInsert<TForm extends Record<string, unknown>>(form: UseFormReturn<TForm>) {
  const last = useRef<{ name: keyof TForm & string; el: HTMLTextAreaElement | HTMLInputElement } | null>(null);
  const track = (name: keyof TForm & string) => ({
    onFocus: (e: React.FocusEvent<HTMLTextAreaElement | HTMLInputElement>) => {
      last.current = { name, el: e.currentTarget };
    },
  });
  const insert = (variable: string, fallback: keyof TForm & string) => {
    const target = last.current ?? { name: fallback, el: null };
    const current = String(form.getValues(target.name as never) ?? "");
    const pos = target.el?.selectionStart ?? current.length;
    const token = `{{${variable}}}`;
    form.setValue(target.name as never, (current.slice(0, pos) + token + current.slice(pos)) as never, { shouldDirty: true, shouldValidate: true });
    requestAnimationFrame(() => {
      target.el?.focus();
      target.el?.setSelectionRange(pos + token.length, pos + token.length);
    });
  };
  return { track, insert };
}

function VariableChips({ onInsert, disabled }: { onInsert: (v: string) => void; disabled?: boolean }) {
  return (
    <div className="flex flex-wrap items-center gap-1 text-[11.5px] text-muted">
      <span className="mr-0.5">Insert:</span>
      {VARIABLES.map((v) => (
        <button
          key={v}
          type="button"
          disabled={disabled}
          onMouseDown={(e) => e.preventDefault()}
          onClick={() => onInsert(v)}
          className="rounded border border-line bg-surface px-1.5 py-px font-mono text-[11px] text-ink-2 hover:border-accent hover:text-accent disabled:opacity-50"
        >
          {v}
        </button>
      ))}
    </div>
  );
}

function Toggle({ label, hint, checked, onChange, disabled }: { label: string; hint?: string; checked: boolean; onChange: (v: boolean) => void; disabled?: boolean }) {
  return (
    <label className="flex items-start justify-between gap-3 rounded-lg border border-line px-3 py-2.5">
      <span className="min-w-0">
        <span className="block text-[13px] font-medium text-ink">{label}</span>
        {hint ? <span className="block text-[12px] text-muted">{hint}</span> : null}
      </span>
      <Switch aria-label={label} checked={checked} onCheckedChange={onChange} disabled={disabled} className="mt-0.5" />
    </label>
  );
}

// ------------------------------------------------------------------ node

const toNodeForm = (n: FlowNode): NodeForm => ({
  name: n.name,
  prompt: n.prompt,
  greeting: n.greeting ?? "",
  allow_interrupt: n.allow_interrupt,
  use_global_rules: n.use_global_rules,
  capture_fields: n.capture_fields.map((f) => ({ ...f })),
  action_ids: [...n.action_ids],
  knowledge_ids: [...n.knowledge_ids],
});

function NodeInspector({ node, issues, warningIssues, dispatch, readOnly, globalRules, agentId, version, dirty }: InspectorProps & { node: FlowNode; issues: FlowIssue[]; warningIssues: FlowIssue[] }) {
  const form = useForm<NodeForm>({ resolver: zodResolver(nodeFormSchema), defaultValues: toNodeForm(node), mode: "onChange" });
  const committed = useRef(JSON.stringify(toNodeForm(node)));
  const { track, insert } = useVariableInsert(form);
  const captures = useFieldArray({ control: form.control, name: "capture_fields" });

  // Undo/redo or a server reset changed this node underneath us: adopt it.
  useEffect(() => {
    const next = JSON.stringify(toNodeForm(node));
    if (next !== committed.current) {
      committed.current = next;
      form.reset(toNodeForm(node));
    }
  }, [node, form]);

  useEffect(() => {
    const sub = form.watch((values, { name }) => {
      if (!name) return;
      const parsed = nodeFormSchema.safeParse(values);
      if (!parsed.success) return;
      const data = { ...parsed.data, greeting: node.kind === "start" ? parsed.data.greeting : null };
      committed.current = JSON.stringify({ ...parsed.data, greeting: data.greeting ?? "" });
      dispatch({ type: "update_node", id: node.id, patch: data, coalesce: name.split(".")[0] });
    });
    return () => sub.unsubscribe();
  }, [form, dispatch, node.id, node.kind]);

  const e = form.formState.errors;
  const rules = globalRules.trim();
  const isEnd = node.kind === "end";

  return (
    <div className="flex flex-col gap-5">
      <header className="flex items-start justify-between gap-2">
        <div>
          <span className={cn("eyebrow", node.kind === "start" ? "text-accent" : isEnd ? "text-success" : undefined)}>{KIND_LABEL[node.kind]}</span>
          <p className="text-[12px] text-muted">
            {node.kind === "start" ? "Where every call begins" : isEnd ? "Says one closing line, then hangs up" : "A part of the conversation with its own goal"}
          </p>
        </div>
        {node.kind !== "start" && !readOnly ? (
          <Button size="icon-sm" variant="ghost" aria-label={`Delete ${node.name}`} onClick={() => dispatch({ type: "delete", selection: { kind: "node", id: node.id } })}>
            <Trash2 />
          </Button>
        ) : null}
      </header>
      <IssueList issues={issues} warningIssues={warningIssues} />
      <fieldset disabled={readOnly} className="flex flex-col gap-5">
        <Field label="Name" error={e.name?.message} required>
          <Input {...form.register("name")} maxLength={LIMITS.name} />
        </Field>
        {node.kind === "start" ? (
          <Field label="Greeting" hint="The first sentence on every call." error={e.greeting?.message} required>
            <Textarea rows={2} {...form.register("greeting")} {...track("greeting")} aria-invalid={issues.some((i) => i.field === "greeting") || undefined} />
          </Field>
        ) : null}
        <Field
          label={isEnd ? "How the agent ends the call" : node.kind === "start" ? "What the agent does once the call starts" : "What the agent does at this step"}
          hint={isEnd ? "One closing reply. Confirm next steps and say goodbye." : "Goal, what to ask, tone, and anything it must or must not say."}
          error={e.prompt?.message}
          required
        >
          <Textarea rows={isEnd ? 4 : 9} className="min-h-28" {...form.register("prompt")} {...track("prompt")} aria-invalid={issues.some((i) => i.field === "prompt") || undefined} />
        </Field>
        <VariableChips disabled={readOnly} onInsert={(v) => insert(v, "prompt")} />

        <div className="flex flex-col gap-2">
          <Toggle
            label="Let people interrupt"
            hint="The agent stops talking when the person speaks over it."
            checked={form.watch("allow_interrupt")}
            onChange={(v) => form.setValue("allow_interrupt", v, { shouldDirty: true, shouldValidate: true })}
          />
          {!isEnd ? (
            <Toggle
              label="Apply global rules"
              hint={rules ? "Global rules are read before this node's text." : "No global rules yet. Add them from the toolbar."}
              checked={form.watch("use_global_rules")}
              onChange={(v) => form.setValue("use_global_rules", v, { shouldDirty: true, shouldValidate: true })}
            />
          ) : null}
        </div>

        <Section
          title="Details to capture here"
          hint={
            isEnd
              ? "Extracted when the call ends. Whether the person opted out is always captured. Values go to the call record and the CRM, not to later prompts."
              : "Extracted when the agent leaves this node. Values go to the call record and the CRM, not to later prompts."
          }
        >
          {captures.fields.length === 0 ? <p className="text-[12px] text-faint">Nothing extra captured here.</p> : null}
          <ul className="flex flex-col gap-2">
            {captures.fields.map((f, i) => (
              <li key={f.id} className="grid grid-cols-[minmax(0,1fr)_auto] gap-2 rounded-lg border border-line p-2">
                <div className="flex flex-col gap-2">
                  <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-2">
                    <div>
                      <Input aria-label="Field name" placeholder="budget" className="font-mono" {...form.register(`capture_fields.${i}.name`)} />
                      {e.capture_fields?.[i]?.name ? <p className="mt-1 text-[11.5px] text-danger">{e.capture_fields[i]?.name?.message}</p> : null}
                    </div>
                    <FormSelect aria-label="Field type" control={form.control} name={`capture_fields.${i}.type`}>
                      <SelectItem value="string">Text</SelectItem>
                      <SelectItem value="number">Number</SelectItem>
                      <SelectItem value="boolean">Yes / no</SelectItem>
                    </FormSelect>
                  </div>
                  <div>
                    <Input aria-label="What to capture" placeholder="Budget they mentioned, with currency" {...form.register(`capture_fields.${i}.description`)} />
                    {e.capture_fields?.[i]?.description ? <p className="mt-1 text-[11.5px] text-danger">{e.capture_fields[i]?.description?.message}</p> : null}
                  </div>
                </div>
                <Button type="button" size="icon-sm" variant="ghost" aria-label="Remove field" onClick={() => captures.remove(i)}>
                  <Trash2 />
                </Button>
              </li>
            ))}
          </ul>
          <Button type="button" size="sm" variant="ghost" className="self-start" disabled={captures.fields.length >= LIMITS.captures} onClick={() => captures.append({ name: "", type: "string", description: "" })}>
            <Plus /> Add field
          </Button>
        </Section>

        {!isEnd ? (
          <>
            <Section title="Actions" hint="What the agent can do here, such as looking up or booking. A path name must not clash with an action's function name.">
              <ActionsPicker value={form.watch("action_ids")} onChange={(ids) => form.setValue("action_ids", ids, { shouldDirty: true, shouldValidate: true })} disabled={readOnly} />
              {issues.some((i) => i.field === "action_ids") ? <p className="text-[12px] text-danger">An attached action no longer exists.</p> : null}
            </Section>
            <Section title="Knowledge" hint="Documents the agent can answer questions from while it is here.">
              <KnowledgePicker value={form.watch("knowledge_ids")} onChange={(ids) => form.setValue("knowledge_ids", ids, { shouldDirty: true, shouldValidate: true })} disabled={readOnly} />
              {issues.some((i) => i.field === "knowledge_ids") ? <p className="text-[12px] text-danger">An attached document no longer exists.</p> : null}
            </Section>
          </>
        ) : null}
      </fieldset>

      <PromptPreview node={node} globalRules={globalRules} agentId={agentId} version={version} dirty={dirty} />
    </div>
  );
}

function composeLocally(node: FlowNode, globalRules: string): { prompt: string; applied: boolean } {
  const rules = globalRules.trim();
  const applied = !!rules && node.use_global_rules && node.kind !== "end";
  return { prompt: [applied ? rules : "", node.prompt.trim()].filter(Boolean).join("\n\n"), applied };
}

function PromptPreview({ node, globalRules, agentId, version, dirty }: { node: FlowNode; globalRules: string; agentId: string; version: number; dirty: boolean }) {
  const [open, setOpen] = useState(false);
  const saved = useCompiledPrompt(agentId, version, node.id, open && !dirty);
  const local = composeLocally(node, globalRules);
  const useSaved = !dirty && saved.data;
  const text = useSaved ? saved.data.prompt : local.prompt;
  const applied = useSaved ? saved.data.global_rules_applied : local.applied;
  return (
    <section className="rounded-lg border border-line">
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)} className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-[12.5px] font-semibold text-ink">
        <span className="inline-flex items-center gap-1.5">
          <Eye className="size-3.5 text-muted" aria-hidden /> Preview prompt
        </span>
        <ChevronDown className={cn("size-4 text-muted transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open ? (
        <div className="border-t border-line px-3 py-2.5">
          <p className="mb-2 text-[11.5px] text-muted">
            Exactly what the model is told at this node{applied ? ", global rules first" : ""}. {dirty ? "Includes your unsaved edits." : saved.isLoading ? "Loading the saved version…" : saved.isError ? "Composed locally." : "From the saved version."} Variables are
            filled per call.
          </p>
          {node.kind === "start" && (node.greeting ?? "").trim() ? (
            <p className="mb-2 rounded-md bg-accent-soft/60 px-2.5 py-1.5 text-[12px] text-accent-soft-ink">
              <span className="eyebrow mr-1.5 text-accent-soft-ink/70">Says first</span>
              {(node.greeting ?? "").trim()}
            </p>
          ) : null}
          <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-md bg-surface-2 px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-ink-2">{text || "Nothing written yet."}</pre>
        </div>
      ) : null}
    </section>
  );
}

// ------------------------------------------------------------------ path

const toPathForm = (p: FlowPath): PathForm => ({ name: p.name, when: p.when, say_while_moving: p.say_while_moving ?? "" });

function PathInspector({ path, issues, warningIssues, state, dispatch, readOnly }: InspectorProps & { path: FlowPath; issues: FlowIssue[]; warningIssues: FlowIssue[] }) {
  const form = useForm<PathForm>({ resolver: zodResolver(pathFormSchema), defaultValues: toPathForm(path), mode: "onChange" });
  const committed = useRef(JSON.stringify(toPathForm(path)));
  const { track, insert } = useVariableInsert(form);
  const from = state.flow.nodes.find((n) => n.id === path.from_node);
  const to = state.flow.nodes.find((n) => n.id === path.to_node);

  useEffect(() => {
    const next = JSON.stringify(toPathForm(path));
    if (next !== committed.current) {
      committed.current = next;
      form.reset(toPathForm(path));
    }
  }, [path, form]);

  useEffect(() => {
    const sub = form.watch((values, { name }) => {
      if (!name) return;
      const parsed = pathFormSchema.safeParse(values);
      if (!parsed.success) return;
      committed.current = JSON.stringify(parsed.data);
      dispatch({ type: "update_path", id: path.id, patch: { name: parsed.data.name, when: parsed.data.when, say_while_moving: parsed.data.say_while_moving.trim() || null }, coalesce: name });
    });
    return () => sub.unsubscribe();
  }, [form, dispatch, path.id]);

  const e = form.formState.errors;
  const fn = pathFunctionName(form.watch("name") || path.name);
  return (
    <div className="flex flex-col gap-5">
      <header className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <span className="eyebrow">Path</span>
          <p className="flex items-center gap-1 truncate text-[12.5px] text-ink-2">
            <span className="truncate">{from?.name ?? "?"}</span>
            <ArrowRight className="size-3.5 shrink-0 text-faint" aria-hidden />
            <span className="truncate">{to?.name ?? "?"}</span>
          </p>
        </div>
        {!readOnly ? (
          <Button size="icon-sm" variant="ghost" aria-label={`Delete path ${path.name}`} onClick={() => dispatch({ type: "delete", selection: { kind: "path", id: path.id } })}>
            <Trash2 />
          </Button>
        ) : null}
      </header>
      <IssueList issues={issues} warningIssues={warningIssues} />
      <fieldset disabled={readOnly} className="flex flex-col gap-5">
        <Field label="Name" hint="Short and distinct from the other paths out of this node." error={e.name?.message} required>
          <Input {...form.register("name")} maxLength={LIMITS.pathName} aria-invalid={issues.some((i) => i.field === "name") || undefined} />
        </Field>
        <p className="-mt-3 text-[12px] text-muted">
          The model takes this path by calling <code className="rounded bg-surface-2 px-1 py-0.5 font-mono text-[11.5px] text-ink-2">{fn || "…"}</code>
        </p>
        <Field label="When to take it" hint="One or two sentences describing the situation. The model reads this to decide." error={e.when?.message} required>
          <Textarea rows={3} {...form.register("when")} {...track("when")} aria-invalid={issues.some((i) => i.field === "when") || undefined} />
        </Field>
        <Field label="Say while moving" hint="Optional: spoken as the agent moves on, so the pause feels natural." error={e.say_while_moving?.message}>
          <Input placeholder="One moment while I check that." {...form.register("say_while_moving")} {...track("say_while_moving")} />
        </Field>
        <VariableChips disabled={readOnly} onInsert={(v) => insert(v, "say_while_moving")} />
      </fieldset>
    </div>
  );
}

// ------------------------------------------------------------------ nothing selected

function FlowSummary({ state, errors, warnings, globalRules, onEditGlobalRules, readOnly }: InspectorProps) {
  const { flow } = state;
  const steps = flow.nodes.filter((n) => n.kind === "step").length;
  const ends = flow.nodes.filter((n) => n.kind === "end").length;
  const errorCount = [...errors.values()].reduce((a, b) => a + b.length, 0);
  const warningCount = [...warnings.values()].reduce((a, b) => a + b.length, 0);
  const rules = globalRules.trim();
  return (
    <div className="flex flex-col gap-5">
      <div>
        <span className="eyebrow">Flow</span>
        <p className="text-[13px] text-ink-2">Select a node or a path to edit it. Drag from a node's right edge to add a path.</p>
      </div>
      <dl className="grid grid-cols-3 gap-2">
        {[
          ["Steps", steps],
          ["Ends", ends],
          ["Paths", flow.paths.length],
        ].map(([label, value]) => (
          <div key={label} className="rounded-lg border border-line px-3 py-2">
            <dt className="text-[11px] text-muted">{label}</dt>
            <dd className="font-mono text-[16px] font-semibold text-ink">{value}</dd>
          </div>
        ))}
      </dl>
      <p className={cn("text-[12.5px]", errorCount ? "text-danger" : warningCount ? "text-warn" : "text-success")}>
        {errorCount ? `${errorCount} ${errorCount === 1 ? "problem" : "problems"} to fix before publishing` : warningCount ? `Ready to publish · ${warningCount} ${warningCount === 1 ? "warning" : "warnings"}` : "Ready to publish"}
      </p>
      <Section title="Global rules" hint="Read before every Start and Step that applies them. Persona, tone, language, what never to say.">
        {rules ? <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-md bg-surface-2 px-2.5 py-2 font-mono text-[11.5px] leading-relaxed text-ink-2">{rules}</pre> : <p className="text-[12px] text-faint">None yet. Without them, each node must repeat persona and tone.</p>}
        <Button size="sm" className="self-start" onClick={onEditGlobalRules} disabled={readOnly && !rules}>
          {readOnly ? "View global rules" : rules ? "Edit global rules" : "Add global rules"}
        </Button>
      </Section>
      <Section title="Keyboard">
        <ul className="flex flex-col gap-1.5 text-[12px] text-muted">
          <li className="flex items-center justify-between gap-2"><span>Undo / redo</span><span className="flex gap-1"><Kbd>Ctrl</Kbd><Kbd>Z</Kbd><span>/</span><Kbd>Ctrl</Kbd><Kbd>Shift</Kbd><Kbd>Z</Kbd></span></li>
          <li className="flex items-center justify-between gap-2"><span>Remove selection</span><Kbd>Del</Kbd></li>
          <li className="flex items-center justify-between gap-2"><span>Clear selection</span><Kbd>Esc</Kbd></li>
          <li className="flex items-center justify-between gap-2"><span>Move between nodes</span><Kbd>Tab</Kbd></li>
        </ul>
      </Section>
    </div>
  );
}
