/**
 * Attach actions and documents to a node. A searchable multi-select: chosen items show as
 * chips, the list comes from the Actions and Knowledge features. When either feature is
 * unavailable the picker says so instead of failing.
 */
import { Command } from "cmdk";
import { BookOpen, Check, ChevronsUpDown, X, Zap } from "lucide-react";
import { useState, type ReactNode } from "react";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { fieldBase } from "@/components/ui/primitives";
import { useActions, useDocuments } from "@/features/voice/flow/queries";
import { errorMessage } from "@/lib/api";
import { cn } from "@/lib/utils";

interface Option {
  id: string;
  label: string;
  description?: string;
  disabled?: boolean;
}

function MultiPicker({
  label,
  icon,
  options,
  value,
  onChange,
  loading,
  unavailable,
  placeholder,
  emptyText,
  disabled,
}: {
  label: string;
  icon: ReactNode;
  options: Option[];
  value: string[];
  onChange: (ids: string[]) => void;
  loading: boolean;
  unavailable: string | null;
  placeholder: string;
  emptyText: string;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const byId = new Map(options.map((o) => [o.id, o]));
  const toggle = (id: string) => onChange(value.includes(id) ? value.filter((v) => v !== id) : [...value, id]);
  return (
    <div className="flex flex-col gap-1.5">
      {value.length ? (
        <ul className="flex flex-wrap gap-1">
          {value.map((id) => {
            const o = byId.get(id);
            return (
              <li key={id} className={cn("inline-flex h-6 items-center gap-1 rounded-[4px] border px-1.5 text-[11.5px] font-medium", o ? "border-line bg-surface-2 text-ink-2" : "border-danger/40 bg-danger-soft text-danger")}>
                <span className="[&_svg]:size-3">{icon}</span>
                <span className="max-w-40 truncate">{o?.label ?? "No longer exists"}</span>
                {!disabled ? (
                  <button type="button" aria-label={`Remove ${o?.label ?? "missing item"}`} onClick={() => toggle(id)} className="grid size-4 place-items-center rounded text-faint hover:text-ink">
                    <X className="size-3" />
                  </button>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}
      {unavailable ? (
        <p className="text-[12px] text-muted">{unavailable}</p>
      ) : (
        <Popover open={open} onOpenChange={setOpen}>
          <PopoverTrigger asChild>
            <button type="button" role="combobox" aria-expanded={open} aria-haspopup="listbox" aria-label={label} disabled={disabled} className={cn(fieldBase, "inline-flex h-8 items-center gap-2 text-left")}>
              <span className="min-w-0 flex-1 truncate text-faint">{placeholder}</span>
              <ChevronsUpDown className="size-3.5 shrink-0 text-faint" aria-hidden />
            </button>
          </PopoverTrigger>
          <PopoverContent className="w-[var(--radix-popover-trigger-width)] min-w-64 p-0">
            <Command loop label={label}>
              <div className="border-b border-line px-2.5">
                <Command.Input placeholder="Search…" className="h-9 w-full bg-transparent text-[13px] text-ink outline-none placeholder:text-faint" />
              </div>
              <Command.List className="max-h-56 overflow-y-auto p-1">
                {loading ? <div role="status" className="px-3 py-6 text-center text-[12.5px] text-muted">Loading…</div> : <Command.Empty className="px-3 py-6 text-center text-[12.5px] text-muted">{emptyText}</Command.Empty>}
                {options.map((o) => {
                  const chosen = value.includes(o.id);
                  return (
                    <Command.Item
                      key={o.id}
                      value={o.id}
                      keywords={[o.label, o.description ?? ""].filter(Boolean)}
                      disabled={o.disabled}
                      onSelect={() => toggle(o.id)}
                      className="flex cursor-default select-none items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-ink-2 outline-none data-[selected=true]:bg-surface-2 data-[selected=true]:text-ink data-[disabled=true]:opacity-50"
                    >
                      <span className={cn("grid size-4 shrink-0 place-items-center rounded-[4px] border", chosen ? "border-accent bg-accent text-accent-ink" : "border-line-strong")}>
                        {chosen ? <Check className="size-3" strokeWidth={3} /> : null}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate">{o.label}</span>
                        {o.description ? <span className="block truncate text-[11.5px] text-muted">{o.description}</span> : null}
                      </span>
                    </Command.Item>
                  );
                })}
              </Command.List>
            </Command>
          </PopoverContent>
        </Popover>
      )}
    </div>
  );
}

export function ActionsPicker({ value, onChange, disabled }: { value: string[]; onChange: (ids: string[]) => void; disabled?: boolean }) {
  const actions = useActions();
  const options: Option[] = (actions.data ?? []).map((a) => ({ id: a.id, label: a.name, description: `${a.function_name} · ${a.description}` }));
  return (
    <MultiPicker
      label="Attach actions"
      icon={<Zap />}
      options={options}
      value={value}
      onChange={onChange}
      loading={actions.isLoading}
      unavailable={actions.isError ? `Actions aren't available right now: ${errorMessage(actions.error)}` : null}
      placeholder={options.length ? "Attach an action…" : "No actions yet. Create them under Actions."}
      emptyText="No matching actions."
      disabled={disabled || (!actions.isLoading && options.length === 0)}
    />
  );
}

export function KnowledgePicker({ value, onChange, disabled }: { value: string[]; onChange: (ids: string[]) => void; disabled?: boolean }) {
  const docs = useDocuments();
  const options: Option[] = (docs.data ?? []).map((d) => ({ id: d.id, label: d.filename, description: d.ready ? "Ready" : d.status === "failed" ? "Processing failed" : "Still processing", disabled: !d.ready }));
  return (
    <MultiPicker
      label="Attach documents"
      icon={<BookOpen />}
      options={options}
      value={value}
      onChange={onChange}
      loading={docs.isLoading}
      unavailable={docs.isError ? `Knowledge isn't available right now: ${errorMessage(docs.error)}` : null}
      placeholder={options.length ? "Attach a document…" : "No documents yet. Upload them under Knowledge."}
      emptyText="No matching documents."
      disabled={disabled || (!docs.isLoading && options.length === 0)}
    />
  );
}
