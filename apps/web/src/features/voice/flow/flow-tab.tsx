/**
 * The Flow tab: toolbar, canvas, a resizable inspector, the validation panel and the
 * global rules editor. Owns the editor reducer and writes every flow change back into the
 * agent form so Save draft / Publish pick it up like any other field.
 */
import { useMutation } from "@tanstack/react-query";
import { ReactFlowProvider, useReactFlow } from "@xyflow/react";
import {
  ChevronDown,
  CircleAlert,
  CircleCheck,
  Flag,
  LayoutGrid,
  Maximize2,
  Plus,
  Redo2,
  ScrollText,
  ShieldCheck,
  TriangleAlert,
  Undo2,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState, type KeyboardEvent } from "react";
import type { UseFormReturn } from "react-hook-form";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field } from "@/components/ui/form";
import { Textarea } from "@/components/ui/primitives";
import { Switch } from "@/components/ui/switch";
import type { AgentForm } from "@/features/voice/agent-form";
import { FlowCanvas, type FocusRequest } from "@/features/voice/flow/canvas";
import { Inspector } from "@/features/voice/flow/inspector";
import { globalRulesSchema } from "@/features/voice/flow/inspector-schema";
import { tidyUp } from "@/features/voice/flow/layout";
import { reducedMotion } from "@/features/voice/flow/motion";
import { createState, reduce, type EditorAction, type EditorState } from "@/features/voice/flow/model";
import { actionNameMap, useActions } from "@/features/voice/flow/queries";
import { TokenMeter } from "@/features/voice/flow/token-meter";
import { KIND_LABEL, LIMITS, type FlowDefinition, type FlowIssue, type IssueTarget } from "@/features/voice/flow/types";
import { issuesByTarget, PROMPT_BUDGET, validateFlow } from "@/features/voice/flow/validate";
import { api, errorMessage, unwrap } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface FlowTabProps {
  agentId: string;
  version: number;
  form: UseFormReturn<AgentForm>;
  canEdit: boolean;
  dirty: boolean;
  /** Set by the page when a publish error names a node or path. */
  focus: FocusRequest | null;
  /**
   * The saved flow for this version, from the server. The tab remounts whenever the
   * version changes and the form is only reset afterwards, so seeding from the form
   * here would show the previous version (or nothing right after a conversion).
   */
  initialFlow: FlowDefinition;
}

const INSPECTOR = { min: 300, max: 560, initial: 380 } as const;

export function FlowTab(props: FlowTabProps) {
  return (
    <ReactFlowProvider>
      <FlowEditor {...props} />
    </ReactFlowProvider>
  );
}

function FlowEditor({ agentId, version, form, canEdit, dirty, focus: externalFocus, initialFlow }: FlowTabProps) {
  const [state, dispatch] = useReducer((s: EditorState, a: EditorAction) => reduce(s, a), initialFlow, createState);
  const { zoomIn, zoomOut, fitView } = useReactFlow();
  const [snap, setSnap] = useState(true);
  const [rulesOpen, setRulesOpen] = useState(false);
  const [panelOpen, setPanelOpen] = useState(false);
  const [focus, setFocus] = useState<FocusRequest | null>(null);
  const [serverIssues, setServerIssues] = useState<{ errors: FlowIssue[]; warnings: FlowIssue[] } | null>(null);
  const [width, setWidth] = useState<number>(INSPECTOR.initial);
  const actions = useActions();
  const globalRules = form.watch("global_rules") ?? "";
  const first = useRef(true);

  // Flow edits are form edits: Save draft / Publish send them, and the page's dirty state reflects them.
  useEffect(() => {
    if (first.current) {
      first.current = false;
      return;
    }
    form.setValue("flow", state.flow, { shouldDirty: true });
    setServerIssues(null);
  }, [state.flow, form]);

  useEffect(() => {
    if (externalFocus) {
      dispatch({ type: "select", selection: externalFocus.target === "flow" ? null : { kind: externalFocus.target, id: externalFocus.id } });
      setFocus(externalFocus);
      setPanelOpen(true);
    }
  }, [externalFocus]);

  const actionNames = useMemo(() => actionNameMap(actions.data), [actions.data]);
  const local = useMemo(() => validateFlow(state.flow, actionNames, globalRules), [state.flow, actionNames, globalRules]);
  const errorList = useMemo(() => [...local.errors, ...(serverIssues?.errors ?? [])], [local.errors, serverIssues]);
  const warningList = useMemo(() => [...local.warnings, ...(serverIssues?.warnings ?? [])], [local.warnings, serverIssues]);
  const errors = useMemo(() => issuesByTarget(errorList), [errorList]);
  const warnings = useMemo(() => issuesByTarget(warningList), [warningList]);

  const serverValidate = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/agents/{agent_id}/validate", { params: { path: { agent_id: agentId } } })),
    onSuccess: (result) => {
      // Rules we already check locally are reported locally; keep only what the server adds.
      const seen = new Set([...local.errors, ...local.warnings].map((i) => `${i.target}:${i.id}:${i.field ?? ""}:${i.message}`));
      // Prompt-budget warnings are estimated locally too; the server's wording may differ, so match on where they point.
      const budgetSeen = new Set(local.warnings.filter((i) => i.field === "prompt" || i.field === "global_rules").map((i) => `${i.target}:${i.id}:${i.field}`));
      const fresh = (list: { target: FlowIssue["target"]; id: string; field?: string | null; message: string }[], budget = false): FlowIssue[] =>
        list
          .filter((i) => !seen.has(`${i.target}:${i.id}:${i.field ?? ""}:${i.message}`) && !(budget && budgetSeen.has(`${i.target}:${i.id}:${i.field ?? ""}`)))
          .map((i) => ({ target: i.target, id: i.id, field: i.field ?? null, message: i.message }));
      setServerIssues({ errors: fresh(result.errors), warnings: fresh(result.warnings, true) });
      setPanelOpen(true);
      if (result.valid && local.valid) toast.success("Ready to publish.");
      else toast.error(`${result.errors.length + local.errors.length} ${result.errors.length + local.errors.length === 1 ? "problem" : "problems"} to fix before publishing.`);
    },
    onError: (e) => toast.error(errorMessage(e)),
  });

  const validate = () => {
    setPanelOpen(true);
    if (dirty) {
      if (local.valid) toast.success("Looks good so far. Save to check attached actions and documents too.");
      else toast.error(`${local.errors.length} ${local.errors.length === 1 ? "problem" : "problems"} to fix before publishing.`);
      return;
    }
    serverValidate.mutate();
  };

  const focusIssue = (issue: FlowIssue) => {
    if (issue.target === "flow") return;
    dispatch({ type: "select", selection: { kind: issue.target, id: issue.id } });
    setFocus({ target: issue.target, id: issue.id, nonce: Date.now() });
  };

  const doDispatch = useCallback((a: EditorAction) => dispatch(a), []);
  const motion = reducedMotion() ? 0 : 200;
  const readOnly = !canEdit;

  const onSeparatorKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "ArrowLeft") setWidth((w) => Math.min(INSPECTOR.max, w + 24));
    if (e.key === "ArrowRight") setWidth((w) => Math.max(INSPECTOR.min, w - 24));
  };
  const onSeparatorDown = (e: React.PointerEvent<HTMLDivElement>) => {
    const startX = e.clientX;
    const startW = width;
    const el = e.currentTarget;
    el.setPointerCapture(e.pointerId);
    const move = (ev: PointerEvent) => setWidth(Math.max(INSPECTOR.min, Math.min(INSPECTOR.max, startW + (startX - ev.clientX))));
    const up = () => {
      el.removeEventListener("pointermove", move);
      el.removeEventListener("pointerup", up);
    };
    el.addEventListener("pointermove", move);
    el.addEventListener("pointerup", up);
  };

  return (
    <div className="flex h-[max(640px,calc(100dvh-300px))] min-h-[640px] flex-col">
      <div className="flex flex-wrap items-center gap-1.5 border-b border-line px-3 py-2" role="toolbar" aria-label="Flow tools">
        <Button size="sm" variant="primary" disabled={readOnly || state.flow.nodes.length >= LIMITS.nodes} onClick={() => dispatch({ type: "add_node", kind: "step" })}>
          <Plus /> Add step
        </Button>
        <Button size="sm" disabled={readOnly || state.flow.nodes.length >= LIMITS.nodes} onClick={() => dispatch({ type: "add_node", kind: "end" })}>
          <Flag /> Add end
        </Button>
        <span className="mx-1 h-5 w-px bg-line" aria-hidden />
        <Button
          size="sm"
          variant="ghost"
          disabled={readOnly || state.flow.nodes.length < 2}
          onClick={() => {
            dispatch({ type: "replace", flow: tidyUp(state.flow) });
            setTimeout(() => void fitView({ padding: 0.25, maxZoom: 1, duration: motion }), 0);
          }}
        >
          <LayoutGrid /> Tidy up
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setRulesOpen(true)}>
          <ScrollText /> Global rules
          {globalRules.trim() ? <span className="size-1.5 rounded-full bg-accent" aria-label="set" /> : null}
        </Button>
        <Button size="sm" variant="ghost" loading={serverValidate.isPending} onClick={validate}>
          <ShieldCheck /> Validate
          {errorList.length ? <span className="rounded-full bg-danger-soft px-1.5 font-mono text-[11px] text-danger">{errorList.length}</span> : null}
        </Button>
        <span className="flex-1" />
        <Button size="icon-sm" variant="ghost" aria-label="Undo" title="Undo (Ctrl+Z)" disabled={!state.past.length || readOnly} onClick={() => dispatch({ type: "undo" })}>
          <Undo2 />
        </Button>
        <Button size="icon-sm" variant="ghost" aria-label="Redo" title="Redo (Ctrl+Shift+Z)" disabled={!state.future.length || readOnly} onClick={() => dispatch({ type: "redo" })}>
          <Redo2 />
        </Button>
        <span className="mx-1 h-5 w-px bg-line" aria-hidden />
        <Button size="icon-sm" variant="ghost" aria-label="Zoom out" onClick={() => void zoomOut({ duration: motion })}>
          <ZoomOut />
        </Button>
        <Button size="icon-sm" variant="ghost" aria-label="Zoom in" onClick={() => void zoomIn({ duration: motion })}>
          <ZoomIn />
        </Button>
        <Button size="icon-sm" variant="ghost" aria-label="Fit to view" onClick={() => void fitView({ padding: 0.25, maxZoom: 1, duration: motion })}>
          <Maximize2 />
        </Button>
        <label className="ml-1 flex items-center gap-1.5 text-[12px] text-muted">
          <Switch size="sm" aria-label="Snap to grid" checked={snap} onCheckedChange={setSnap} /> Snap
        </label>
      </div>

      <div className="flex min-h-0 flex-1">
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="min-h-0 flex-1">
            <FlowCanvas agentId={agentId} state={state} dispatch={doDispatch} errors={errors} warnings={warnings} snap={snap} readOnly={readOnly} focus={focus} />
          </div>
          <ValidationPanel open={panelOpen} onToggle={() => setPanelOpen((o) => !o)} errors={errorList} warnings={warningList} flow={state.flow} onFocus={focusIssue} />
        </div>
        <div
          role="separator"
          aria-orientation="vertical"
          aria-label="Resize inspector"
          aria-valuenow={width}
          aria-valuemin={INSPECTOR.min}
          aria-valuemax={INSPECTOR.max}
          tabIndex={0}
          onKeyDown={onSeparatorKey}
          onPointerDown={onSeparatorDown}
          className="w-1.5 shrink-0 cursor-col-resize border-l border-line bg-surface hover:bg-accent-soft focus-visible:bg-accent-soft"
        />
        <aside className="shrink-0 overflow-y-auto bg-surface px-4 py-4" style={{ width }} aria-label="Inspector">
          <Inspector
            state={state}
            dispatch={doDispatch}
            errors={errors}
            warnings={warnings}
            readOnly={readOnly}
            globalRules={globalRules}
            onEditGlobalRules={() => setRulesOpen(true)}
            agentId={agentId}
            version={version}
            dirty={dirty}
          />
        </aside>
      </div>

      <GlobalRulesDialog open={rulesOpen} onOpenChange={setRulesOpen} form={form} readOnly={readOnly} />
    </div>
  );
}

function ValidationPanel({ open, onToggle, errors, warnings, flow, onFocus }: { open: boolean; onToggle: () => void; errors: FlowIssue[]; warnings: FlowIssue[]; flow: FlowDefinition; onFocus: (i: FlowIssue) => void }) {
  const label = (i: FlowIssue) => {
    if (i.target === "node") {
      const n = flow.nodes.find((x) => x.id === i.id);
      return n ? `${KIND_LABEL[n.kind]} · ${n.name}` : "Node";
    }
    if (i.target === "path") {
      const p = flow.paths.find((x) => x.id === i.id);
      return p ? `Path · ${p.name}` : "Path";
    }
    return i.field === "global_rules" ? "Global rules" : "Flow";
  };
  const summary = errors.length ? `${errors.length} ${errors.length === 1 ? "problem" : "problems"}` : "No problems";
  const extra = warnings.length ? ` · ${warnings.length} ${warnings.length === 1 ? "warning" : "warnings"}` : "";
  return (
    <section className="border-t border-line bg-surface" aria-label="Validation">
      <button type="button" aria-expanded={open} onClick={onToggle} className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-[12.5px] font-medium">
        {errors.length ? <CircleAlert className="size-4 text-danger" aria-hidden /> : warnings.length ? <TriangleAlert className="size-4 text-warn" aria-hidden /> : <CircleCheck className="size-4 text-success" aria-hidden />}
        <span className={cn(errors.length ? "text-danger" : "text-ink-2")}>
          {summary}
          {extra}
        </span>
        <span className="flex-1" />
        <ChevronDown className={cn("size-4 text-muted transition-transform", open && "rotate-180")} aria-hidden />
      </button>
      {open && (errors.length || warnings.length) ? (
        <ul className="max-h-40 divide-y divide-line overflow-y-auto border-t border-line">
          {[...errors.map((i) => ({ i, kind: "error" as const })), ...warnings.map((i) => ({ i, kind: "warning" as const }))].map(({ i, kind }, n) => (
            <li key={n}>
              <button
                type="button"
                disabled={i.target === "flow"}
                onClick={() => onFocus(i)}
                className="flex w-full items-start gap-2 px-3 py-1.5 text-left text-[12.5px] hover:bg-surface-2 disabled:cursor-default disabled:hover:bg-transparent"
              >
                {kind === "error" ? <CircleAlert className="mt-0.5 size-3.5 shrink-0 text-danger" aria-hidden /> : <TriangleAlert className="mt-0.5 size-3.5 shrink-0 text-warn" aria-hidden />}
                <span className="shrink-0 font-medium text-ink">{label(i)}</span>
                <span className="min-w-0 text-ink-2">{i.message}</span>
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

function GlobalRulesDialog({ open, onOpenChange, form, readOnly }: { open: boolean; onOpenChange: (o: boolean) => void; form: UseFormReturn<AgentForm>; readOnly: boolean }) {
  const value = form.watch("global_rules") ?? "";
  const parsed = globalRulesSchema.safeParse(value);
  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="Global rules"
      description="Read before every Start and Step that applies them, so each node only describes its own goal. Put persona, tone, language and hard limits here."
      width="max-w-2xl"
      footer={<Button variant="primary" onClick={() => onOpenChange(false)}>Done</Button>}
    >
      <Field label="Rules" error={parsed.success ? undefined : parsed.error.issues[0]?.message} hint={`${value.length.toLocaleString()} / ${LIMITS.globalRules.toLocaleString()} characters`}>
        <Textarea
          rows={12}
          className="min-h-64 font-mono text-[12.5px]"
          value={value}
          readOnly={readOnly}
          placeholder={"You are Sarah, calling on behalf of Acme.\n- Keep each reply to one or two sentences.\n- Never invent prices or availability.\n- If asked, say you are an AI assistant."}
          onChange={(e) => form.setValue("global_rules", e.target.value, { shouldDirty: true })}
        />
      </Field>
      <TokenMeter text={value} budget={PROMPT_BUDGET.globalRules} what="global rules" className="mt-3" />
    </Dialog>
  );
}

export type { IssueTarget };
