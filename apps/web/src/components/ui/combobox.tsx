import { Command } from "cmdk";
import { Check, ChevronsUpDown, Loader2, X } from "lucide-react";
import { forwardRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Controller, type Control, type FieldPath, type FieldValues } from "react-hook-form";

import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { fieldBase } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

export interface ComboboxOption {
  value: string;
  label: string;
  description?: string;
  icon?: ReactNode;
  /** Extra text the search should match, beyond label and description. */
  keywords?: string[];
  /** Rendered at the end of the row (e.g. a preview button). Must stop click propagation. */
  trailing?: ReactNode;
  disabled?: boolean;
}

export interface ComboboxProps {
  options: ComboboxOption[];
  value: string | null | undefined;
  /** `null` when cleared. */
  onValueChange: (value: string | null) => void;
  placeholder?: string;
  searchPlaceholder?: string;
  emptyText?: string;
  loading?: boolean;
  /** When set, filtering is left to the caller (async lists); the list shows `options` as given. */
  onSearchChange?: (query: string) => void;
  allowClear?: boolean;
  /** Shown when the selected value isn't among `options` (async lists, stale ids). */
  valueLabel?: string;
  disabled?: boolean;
  size?: "sm" | "md";
  invalid?: boolean;
  className?: string;
  contentClassName?: string;
  align?: "start" | "end";
  id?: string;
  onBlur?: () => void;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
  "aria-required"?: boolean;
}

/** Searchable single select for long or remote lists (owners, leads, voices). */
export const Combobox = forwardRef<HTMLButtonElement, ComboboxProps>(function Combobox(
  {
    options,
    value,
    onValueChange,
    placeholder = "Choose…",
    searchPlaceholder = "Search…",
    emptyText = "No matches.",
    loading,
    onSearchChange,
    allowClear,
    valueLabel,
    disabled,
    size = "md",
    invalid,
    className,
    contentClassName,
    align = "start",
    id,
    onBlur,
    ...aria
  },
  ref,
) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const selected = value ? options.find((o) => o.value === value) : undefined;
  const display = selected?.label ?? (value ? (valueLabel ?? value) : null);

  const change = (open: boolean) => {
    setOpen(open);
    if (!open) {
      setSearch("");
      onSearchChange?.("");
      onBlur?.();
    }
  };
  const onTriggerKeyDown = (e: KeyboardEvent<HTMLButtonElement>) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      setOpen(true);
    }
  };

  return (
    <div className={cn("relative", className)}>
      <Popover open={open} onOpenChange={change}>
        <PopoverTrigger asChild>
          <button
            ref={ref}
            type="button"
            id={id}
            role="combobox"
            aria-expanded={open}
            aria-haspopup="listbox"
            aria-label={aria["aria-label"]}
            aria-labelledby={aria["aria-labelledby"]}
            aria-invalid={invalid || aria["aria-invalid"] || undefined}
            aria-describedby={aria["aria-describedby"]}
            aria-required={aria["aria-required"]}
            disabled={disabled}
            onKeyDown={onTriggerKeyDown}
            className={cn(
              fieldBase,
              "inline-flex items-center gap-2 text-left",
              size === "sm" ? "h-7 px-2 text-[12.5px]" : "h-8",
              allowClear && value ? "pr-12" : "pr-7",
            )}
          >
            {selected?.icon ? <span className="shrink-0 [&_svg]:size-4">{selected.icon}</span> : null}
            <span className={cn("min-w-0 flex-1 truncate", display === null && "text-faint")}>{display ?? placeholder}</span>
            <ChevronsUpDown className="absolute right-2 top-1/2 size-3.5 -translate-y-1/2 text-faint" aria-hidden />
          </button>
        </PopoverTrigger>
        <PopoverContent align={align} className={cn("w-[var(--radix-popover-trigger-width)] min-w-56 p-0", contentClassName)}>
          <Command shouldFilter={!onSearchChange} loop label={aria["aria-label"]}>
            <div className="flex items-center gap-2 border-b border-line px-2.5">
              <Command.Input
                value={search}
                onValueChange={(q) => {
                  setSearch(q);
                  onSearchChange?.(q);
                }}
                placeholder={searchPlaceholder}
                className="h-9 min-w-0 flex-1 bg-transparent text-[13px] text-ink outline-none placeholder:text-faint"
              />
              {loading ? <Loader2 className="size-3.5 animate-spin text-muted" aria-label="Loading" /> : null}
            </div>
            <Command.List className="max-h-64 overflow-y-auto p-1">
              {!loading ? <Command.Empty className="px-3 py-6 text-center text-[12.5px] text-muted">{emptyText}</Command.Empty> : null}
              {loading && options.length === 0 ? (
                <div role="status" className="px-3 py-6 text-center text-[12.5px] text-muted">
                  Loading…
                </div>
              ) : null}
              {options.map((o) => (
                <Command.Item
                  key={o.value}
                  value={o.value}
                  keywords={[o.label, o.description ?? "", ...(o.keywords ?? [])].filter(Boolean)}
                  disabled={o.disabled}
                  onSelect={() => {
                    onValueChange(o.value);
                    change(false);
                  }}
                  className={cn(
                    "flex cursor-default select-none items-center gap-2 rounded-md px-2 py-1.5 text-[13px] text-ink-2 outline-none",
                    "data-[selected=true]:bg-surface-2 data-[selected=true]:text-ink data-[disabled=true]:opacity-50 [&_svg]:shrink-0",
                  )}
                >
                  {o.icon ? <span className="shrink-0 [&_svg]:size-4">{o.icon}</span> : null}
                  <span className="min-w-0 flex-1">
                    <span className={cn("block truncate", o.value === value && "font-medium text-ink")}>{o.label}</span>
                    {o.description ? <span className="block truncate text-[11.5px] text-muted">{o.description}</span> : null}
                  </span>
                  {o.trailing}
                  <Check className={cn("size-3.5 text-accent", o.value === value ? "opacity-100" : "opacity-0")} aria-hidden />
                </Command.Item>
              ))}
            </Command.List>
          </Command>
        </PopoverContent>
      </Popover>
      {allowClear && value && !disabled ? (
        <button
          type="button"
          aria-label="Clear"
          onClick={() => onValueChange(null)}
          className="absolute right-6 top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded text-faint hover:bg-surface-2 hover:text-ink"
        >
          <X className="size-3.5" />
        </button>
      ) : null}
    </div>
  );
});

/** react-hook-form binding; the field holds `""` when nothing is chosen. */
export function FormCombobox<
  TFieldValues extends FieldValues,
  TName extends FieldPath<TFieldValues>,
  TContext = unknown,
  TTransformed extends FieldValues = TFieldValues,
>({
  control,
  name,
  ...props
}: Omit<ComboboxProps, "value" | "onValueChange" | "onBlur"> & { control: Control<TFieldValues, TContext, TTransformed>; name: TName }) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field, fieldState }) => (
        <Combobox
          {...props}
          ref={field.ref}
          value={typeof field.value === "string" ? field.value : ""}
          onValueChange={(v) => field.onChange(v ?? "")}
          onBlur={field.onBlur}
          disabled={props.disabled ?? field.disabled}
          invalid={props.invalid ?? fieldState.invalid}
        />
      )}
    />
  );
}
