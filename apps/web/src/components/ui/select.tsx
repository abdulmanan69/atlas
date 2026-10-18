import { Check, ChevronDown, ChevronUp } from "lucide-react";
import { Select as SelectPrimitive } from "radix-ui";
import { forwardRef, type ReactNode } from "react";
import { Controller, type Control, type FieldPath, type FieldValues } from "react-hook-form";

import { popSurface } from "@/components/ui/popover";
import { fieldBase } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

/**
 * Radix forbids items with an empty-string value, so the "none" option carries this
 * sentinel internally. Callers only ever see `""`.
 */
export const SELECT_EMPTY = "__none__";

export interface SelectProps {
  /** `""`, `null` or `undefined` all mean "nothing chosen". */
  value: string | null | undefined;
  /** Receives `""` when the empty option is chosen. */
  onValueChange: (value: string) => void;
  children: ReactNode;
  placeholder?: string;
  disabled?: boolean;
  size?: "sm" | "md";
  invalid?: boolean;
  /** Adds a first option that maps to `""`. */
  allowEmpty?: boolean;
  emptyLabel?: string;
  className?: string;
  contentClassName?: string;
  align?: "start" | "end";
  id?: string;
  name?: string;
  onBlur?: () => void;
  onOpenChange?: (open: boolean) => void;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
  "aria-required"?: boolean;
}

export const Select = forwardRef<HTMLButtonElement, SelectProps>(function Select(
  {
    value,
    onValueChange,
    children,
    placeholder,
    disabled,
    size = "md",
    invalid,
    allowEmpty,
    emptyLabel = "None",
    className,
    contentClassName,
    align = "start",
    id,
    name,
    onBlur,
    onOpenChange,
    ...aria
  },
  ref,
) {
  const internal = value ? value : allowEmpty ? SELECT_EMPTY : "";
  return (
    <SelectPrimitive.Root
      value={internal}
      onValueChange={(v) => onValueChange(v === SELECT_EMPTY ? "" : v)}
      disabled={disabled}
      name={name}
      onOpenChange={onOpenChange}
    >
      <SelectPrimitive.Trigger
        ref={ref}
        id={id}
        onBlur={onBlur}
        aria-invalid={invalid || aria["aria-invalid"] || undefined}
        aria-label={aria["aria-label"]}
        aria-labelledby={aria["aria-labelledby"]}
        aria-describedby={aria["aria-describedby"]}
        aria-required={aria["aria-required"]}
        className={cn(
          fieldBase,
          "inline-flex items-center justify-between gap-2 text-left data-[placeholder]:text-faint [&>span:first-child]:min-w-0 [&>span:first-child]:flex-1 [&>span:first-child]:truncate",
          size === "sm" ? "h-7 px-2 text-[12.5px]" : "h-8",
          className,
        )}
      >
        <SelectPrimitive.Value placeholder={placeholder} />
        <SelectPrimitive.Icon asChild>
          <ChevronDown className="size-4 shrink-0 text-faint" aria-hidden />
        </SelectPrimitive.Icon>
      </SelectPrimitive.Trigger>
      <SelectPrimitive.Portal>
        <SelectPrimitive.Content
          position="popper"
          align={align}
          sideOffset={4}
          collisionPadding={8}
          className={cn(
            popSurface,
            "max-h-[min(360px,var(--radix-select-content-available-height))] min-w-[var(--radix-select-trigger-width)] overflow-hidden",
            contentClassName,
          )}
        >
          <SelectPrimitive.ScrollUpButton className="flex h-6 items-center justify-center bg-surface text-muted">
            <ChevronUp className="size-4" aria-hidden />
          </SelectPrimitive.ScrollUpButton>
          <SelectPrimitive.Viewport className="p-1">
            {allowEmpty ? <SelectItem value={SELECT_EMPTY}>{emptyLabel}</SelectItem> : null}
            {children}
          </SelectPrimitive.Viewport>
          <SelectPrimitive.ScrollDownButton className="flex h-6 items-center justify-center bg-surface text-muted">
            <ChevronDown className="size-4" aria-hidden />
          </SelectPrimitive.ScrollDownButton>
        </SelectPrimitive.Content>
      </SelectPrimitive.Portal>
    </SelectPrimitive.Root>
  );
});

export function SelectItem({
  value,
  children,
  description,
  disabled,
  textValue,
  className,
}: {
  value: string;
  children: ReactNode;
  description?: ReactNode;
  disabled?: boolean;
  /** Text used for typeahead when children aren't plain text. */
  textValue?: string;
  className?: string;
}) {
  return (
    <SelectPrimitive.Item
      value={value}
      disabled={disabled}
      textValue={textValue}
      className={cn(
        "relative flex cursor-default select-none flex-col justify-center rounded-md py-1.5 pl-2 pr-8 text-[13px] text-ink-2 outline-none",
        "data-[highlighted]:bg-surface-2 data-[highlighted]:text-ink data-[state=checked]:text-ink data-[disabled]:opacity-50",
        className,
      )}
    >
      <SelectPrimitive.ItemText>{children}</SelectPrimitive.ItemText>
      {description ? <span className="text-[11.5px] text-muted">{description}</span> : null}
      <SelectPrimitive.ItemIndicator className="absolute right-2 top-1/2 -translate-y-1/2">
        <Check className="size-3.5 text-accent" aria-hidden />
      </SelectPrimitive.ItemIndicator>
    </SelectPrimitive.Item>
  );
}

export function SelectGroup({ label, children }: { label: string; children: ReactNode }) {
  return (
    <SelectPrimitive.Group>
      <SelectPrimitive.Label className="px-2 pb-1 pt-1.5 text-[11px] font-semibold uppercase tracking-wider text-faint">{label}</SelectPrimitive.Label>
      {children}
    </SelectPrimitive.Group>
  );
}

export function SelectSeparator() {
  return <SelectPrimitive.Separator className="my-1 h-px bg-line" />;
}

/**
 * react-hook-form binding. Keeps zod validation and `shouldFocusError` working:
 *
 *   <Field label="Stage" error={errors.stage_id?.message}>
 *     <FormSelect control={form.control} name="stage_id" allowEmpty emptyLabel="Default">
 *       {stages.map((s) => <SelectItem key={s.id} value={s.id}>{s.name}</SelectItem>)}
 *     </FormSelect>
 *   </Field>
 */
export function FormSelect<
  TFieldValues extends FieldValues,
  TName extends FieldPath<TFieldValues>,
  TContext = unknown,
  TTransformed extends FieldValues = TFieldValues,
>({
  control,
  name,
  ...props
}: Omit<SelectProps, "value" | "onValueChange" | "name" | "onBlur"> & { control: Control<TFieldValues, TContext, TTransformed>; name: TName }) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field, fieldState }) => (
        <Select
          {...props}
          ref={field.ref}
          name={field.name}
          value={typeof field.value === "string" ? field.value : ""}
          onValueChange={field.onChange}
          onBlur={field.onBlur}
          disabled={props.disabled ?? field.disabled}
          invalid={props.invalid ?? fieldState.invalid}
        />
      )}
    />
  );
}
