/** Agent variables: fixed values every prompt can use as {{name}}, next to the lead's fields. */
import { Plus, Trash2 } from "lucide-react";
import { useFieldArray, type UseFormReturn } from "react-hook-form";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/primitives";
import { MAX_VARIABLES, VARIABLES, type AgentForm } from "@/features/voice/agent-form";

export function VariablesSection({ form }: { form: UseFormReturn<AgentForm> }) {
  const rows = useFieldArray({ control: form.control, name: "variables" });
  const e = form.formState.errors.variables;
  const values = form.watch("variables");
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-muted">
        From the lead on every call:
        {VARIABLES.map((v) => (
          <code key={v} className="rounded bg-surface-2 px-1.5 py-0.5 font-mono text-[11.5px] text-ink-2">{`{{${v}}}`}</code>
        ))}
      </div>
      {rows.fields.length ? (
        <ul className="flex flex-col gap-2" aria-label="Agent variables">
          {rows.fields.map((f, i) => (
            <li key={f.id} className="grid grid-cols-[minmax(0,180px)_minmax(0,1fr)_auto] items-start gap-2">
              <div>
                <Input aria-label={`Variable ${i + 1} name`} placeholder="office_address" className="font-mono" {...form.register(`variables.${i}.name`)} aria-invalid={e?.[i]?.name ? true : undefined} />
                {e?.[i]?.name ? (
                  <p role="alert" className="mt-1 text-[11.5px] text-danger">
                    {e[i]?.name?.message}
                  </p>
                ) : values[i]?.name ? (
                  <p className="mt-1 font-mono text-[11px] text-muted">{`{{${values[i]?.name}}}`}</p>
                ) : null}
              </div>
              <div>
                <Input aria-label={`Variable ${i + 1} value`} placeholder="12 Main Boulevard, Gulberg" {...form.register(`variables.${i}.value`)} aria-invalid={e?.[i]?.value ? true : undefined} />
                {e?.[i]?.value ? (
                  <p role="alert" className="mt-1 text-[11.5px] text-danger">
                    {e[i]?.value?.message}
                  </p>
                ) : null}
              </div>
              <Button type="button" size="icon-sm" variant="ghost" aria-label={`Remove variable ${i + 1}`} onClick={() => rows.remove(i)}>
                <Trash2 />
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-[12.5px] text-muted">No agent variables yet. Add values you repeat across prompts, such as an office address or this month's offer.</p>
      )}
      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" size="sm" onClick={() => rows.append({ name: "", value: "" })} disabled={rows.fields.length >= MAX_VARIABLES}>
          <Plus /> Add variable
        </Button>
        <span className="text-[12px] text-muted">
          {rows.fields.length} / {MAX_VARIABLES}. A custom lead field with the same name and a value wins on that call.
        </span>
      </div>
      {typeof e?.message === "string" ? <p className="text-[12px] text-danger">{e.message}</p> : null}
    </div>
  );
}
