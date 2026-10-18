import { useMutation } from "@tanstack/react-query";
import { CheckCircle2, CircleX, Play } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, FormError } from "@/components/ui/form";
import { Badge, Input, Textarea } from "@/components/ui/primitives";
import { Switch } from "@/components/ui/switch";
import type { Action } from "@/features/actions/action-schema";
import { api, errorMessage, unwrap, type Schemas } from "@/lib/api";
import { cn } from "@/lib/utils";

type TestResult = Schemas["ActionTestResult"];
type ParamType = Schemas["ActionParameter"]["type"];

/** Turn the sample text into the typed value the model would send. */
function coerce(type: ParamType | undefined, raw: string | boolean): { value?: unknown; error?: string } {
  if (type === "boolean") return { value: raw === true || raw === "true" };
  const text = typeof raw === "string" ? raw : "";
  if (text.trim() === "") return { value: undefined };
  if (type === "number") {
    const n = Number(text);
    return Number.isFinite(n) ? { value: n } : { error: "Enter a number" };
  }
  if (type === "object" || type === "array") {
    try {
      const parsed: unknown = JSON.parse(text);
      const ok = type === "array" ? Array.isArray(parsed) : typeof parsed === "object" && parsed !== null && !Array.isArray(parsed);
      return ok ? { value: parsed } : { error: type === "array" ? "Enter a JSON array, e.g. [1, 2]" : "Enter a JSON object, e.g. {\"a\": 1}" };
    } catch {
      return { error: "Not valid JSON" };
    }
  }
  return { value: text };
}

export function ActionTestDialog({ action, onOpenChange }: { action: Action | null; onOpenChange: (open: boolean) => void }) {
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [presetValues, setPresetValues] = useState<Record<string, string | boolean>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [result, setResult] = useState<TestResult | null>(null);

  const run = useMutation({
    mutationFn: (body: Schemas["ActionTestRequest"]) => unwrap(api.POST("/api/v1/actions/{action_id}/test", { params: { path: { action_id: action!.id } }, body })),
    onSuccess: setResult,
  });

  const submit = () => {
    if (!action) return;
    const errs: Record<string, string> = {};
    const build = (list: { name: string; type?: ParamType }[], source: Record<string, string | boolean>, prefix: string) => {
      const out: Record<string, unknown> = {};
      for (const p of list) {
        const { value, error } = coerce(p.type, source[p.name] ?? (p.type === "boolean" ? false : ""));
        if (error) errs[`${prefix}${p.name}`] = error;
        else if (value !== undefined) out[p.name] = value;
      }
      return out;
    };
    const parameters = build(action.parameters ?? [], values, "p:");
    const preset_parameters = build(action.preset_parameters ?? [], presetValues, "x:");
    setFieldErrors(errs);
    if (Object.keys(errs).length) return;
    setResult(null);
    run.mutate({ parameters, preset_parameters });
  };

  const close = (o: boolean) => {
    onOpenChange(o);
    if (!o) {
      setValues({});
      setPresetValues({});
      setFieldErrors({});
      setResult(null);
      run.reset();
    }
  };

  const params = action?.parameters ?? [];
  const presets = action?.preset_parameters ?? [];

  return (
    <Dialog
      open={action !== null}
      onOpenChange={close}
      title={`Test ${action?.name ?? ""}`}
      description="Runs the request now with the sample values below and shows what came back. The destination is checked against the policy first."
      width="max-w-xl"
      footer={
        <>
          <Button onClick={() => close(false)}>Close</Button>
          <Button variant="primary" loading={run.isPending} onClick={submit}>
            <Play /> Run test
          </Button>
        </>
      }
    >
      {action ? (
        <form
          className="flex flex-col gap-5"
          onSubmit={(e) => {
            e.preventDefault();
            submit();
          }}
        >
          <FormError message={run.error ? errorMessage(run.error) : null} />
          <p className="flex items-center gap-2 font-mono text-[12px] text-muted">
            <Badge className="font-mono">{action.method}</Badge>
            <span className="truncate">{action.url}</span>
          </p>
          {params.length ? (
            <section className="flex flex-col gap-3">
              <h3 className="eyebrow">Sample parameter values</h3>
              {params.map((p) => (
                <SampleInput key={p.name} name={p.name} type={p.type ?? "string"} description={p.description} required={p.required ?? true} value={values[p.name]} error={fieldErrors[`p:${p.name}`]} onChange={(v) => setValues({ ...values, [p.name]: v })} />
              ))}
            </section>
          ) : null}
          {presets.length ? (
            <section className="flex flex-col gap-3">
              <h3 className="eyebrow">Preset values</h3>
              <p className="-mt-1 text-[12px] text-muted">On a real call these come from the call context. Give sample values here.</p>
              {presets.map((p) => (
                <SampleInput key={p.name} name={p.name} type={p.type ?? "string"} description={`Template: ${p.value}`} required={p.required ?? true} value={presetValues[p.name]} error={fieldErrors[`x:${p.name}`]} onChange={(v) => setPresetValues({ ...presetValues, [p.name]: v })} />
              ))}
            </section>
          ) : null}
          {!params.length && !presets.length ? <p className="text-[13px] text-muted">This action takes no parameters. Run it to see the response.</p> : null}
          {result ? <ResultPanel result={result} /> : null}
          <button type="submit" hidden />
        </form>
      ) : null}
    </Dialog>
  );
}

function SampleInput({
  name,
  type,
  description,
  required,
  value,
  error,
  onChange,
}: {
  name: string;
  type: ParamType;
  description: string;
  required: boolean;
  value: string | boolean | undefined;
  error?: string;
  onChange: (v: string | boolean) => void;
}) {
  const label = `${name} · ${type}`;
  if (type === "boolean") {
    return (
      <div className="flex items-center justify-between gap-4">
        <div>
          <p className="font-mono text-[13px] text-ink">{name}</p>
          <p className="text-[12px] text-muted">{description}</p>
        </div>
        <Switch aria-label={name} checked={value === true} onCheckedChange={onChange} />
      </div>
    );
  }
  const text = typeof value === "string" ? value : "";
  return (
    <Field label={label} required={required} hint={description} error={error}>
      {type === "object" || type === "array" ? (
        <Textarea value={text} onChange={(e) => onChange(e.target.value)} rows={3} className="font-mono text-[12px]" placeholder={type === "array" ? "[]" : "{}"} spellCheck={false} />
      ) : (
        <Input value={text} onChange={(e) => onChange(e.target.value)} className="font-mono" inputMode={type === "number" ? "decimal" : undefined} spellCheck={false} />
      )}
    </Field>
  );
}

export function ResultPanel({ result }: { result: TestResult }) {
  return (
    <section className="rounded-lg border border-line" aria-label="Test result" role="status">
      <div className={cn("flex flex-wrap items-center gap-3 border-b border-line px-3 py-2 text-[13px]", result.ok ? "text-success" : "text-danger")}>
        {result.ok ? <CheckCircle2 className="size-4" aria-hidden /> : <CircleX className="size-4" aria-hidden />}
        <span className="font-semibold">{result.ok ? "Succeeded" : "Failed"}</span>
        <span className="font-mono text-[12px] text-ink-2">{result.status_code !== null ? `HTTP ${result.status_code}` : "No response"}</span>
        <span className="font-mono text-[12px] text-muted">{result.latency_ms} ms</span>
        <span className="ml-auto truncate font-mono text-[11.5px] text-muted">
          {result.request_method} {result.request_url}
        </span>
      </div>
      {result.error ? <p className="border-b border-line px-3 py-2 text-[12.5px] text-danger">{result.error}</p> : null}
      {result.hint ? <p className="border-b border-line bg-warn-soft px-3 py-2 text-[12.5px] text-warn">{result.hint}</p> : null}
      {result.response_excerpt ? (
        <pre className="max-h-56 overflow-auto px-3 py-2 font-mono text-[11.5px] leading-relaxed text-ink-2 whitespace-pre-wrap break-all">{result.response_excerpt}</pre>
      ) : (
        <p className="px-3 py-2 text-[12.5px] text-muted">Empty response body.</p>
      )}
    </section>
  );
}
