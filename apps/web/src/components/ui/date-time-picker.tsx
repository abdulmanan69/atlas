import { CalendarClock, ChevronLeft, ChevronRight, X } from "lucide-react";
import { forwardRef, useId, useState, type KeyboardEvent } from "react";
import { DayPicker, type ClassNames } from "react-day-picker";
import { Controller, type Control, type FieldPath, type FieldValues } from "react-hook-form";

import { Button } from "@/components/ui/button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { fieldBase } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

const displayFmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
const captionFmt = new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" });
const weekdayFmt = new Intl.DateTimeFormat(undefined, { weekday: "short" });

function firstDayOfWeek(): 0 | 1 | 2 | 3 | 4 | 5 | 6 {
  try {
    const info = (new Intl.Locale(navigator.language) as { getWeekInfo?: () => { firstDay: number } }).getWeekInfo?.();
    if (info) return (info.firstDay % 7) as 0 | 1 | 2 | 3 | 4 | 5 | 6;
  } catch {
    /* fall through */
  }
  return 1;
}

export type DatePreset = "today" | "tomorrow" | "next-week";
export const PRESETS: { id: DatePreset; label: string }[] = [
  { id: "today", label: "Today 9:00" },
  { id: "tomorrow", label: "Tomorrow 9:00" },
  { id: "next-week", label: "Next week" },
];

/** Local 09:00 on the preset's day. "Next week" is seven days out. */
export function presetDate(preset: DatePreset, now: Date = new Date()): Date {
  const d = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 9, 0, 0, 0);
  if (preset === "tomorrow") d.setDate(d.getDate() + 1);
  if (preset === "next-week") d.setDate(d.getDate() + 7);
  return d;
}

export function withTime(day: Date, hours: number, minutes: number): Date {
  return new Date(day.getFullYear(), day.getMonth(), day.getDate(), hours, minutes, 0, 0);
}

const parseIso = (iso: string | null | undefined): Date | null => {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
};

const pad = (n: number) => String(n).padStart(2, "0");

const dayPickerClasses: Partial<ClassNames> = {
  root: "relative select-none",
  months: "flex flex-col",
  month: "flex flex-col gap-1",
  month_caption: "flex h-8 items-center justify-center px-8",
  caption_label: "text-[13px] font-semibold text-ink",
  nav: "absolute inset-x-0 top-0 flex h-8 items-center justify-between",
  button_previous: "grid size-7 place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-40",
  button_next: "grid size-7 place-items-center rounded-md text-muted hover:bg-surface-2 hover:text-ink disabled:opacity-40",
  chevron: "size-4",
  month_grid: "w-full border-collapse",
  weekdays: "",
  weekday: "h-7 w-8 text-center text-[10.5px] font-semibold uppercase tracking-wider text-faint",
  week: "",
  day: "group p-0 text-center",
  day_button:
    "size-8 rounded-md text-[13px] text-ink-2 transition-colors hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 " +
    "group-data-[today=true]:font-semibold group-data-[today=true]:text-accent " +
    "group-data-[selected=true]:bg-accent group-data-[selected=true]:text-accent-ink group-data-[selected=true]:hover:bg-accent-hover " +
    "group-data-[outside=true]:text-faint group-data-[disabled=true]:pointer-events-none group-data-[disabled=true]:opacity-30",
  selected: "",
  today: "",
  outside: "",
  disabled: "",
  hidden: "invisible",
  focused: "",
};

export interface DateTimePickerProps {
  /** ISO 8601 string (UTC) or `null`. Displayed in the user's locale and time zone. */
  value: string | null | undefined;
  onChange: (iso: string | null) => void;
  clearable?: boolean;
  disabled?: boolean;
  placeholder?: string;
  size?: "sm" | "md";
  invalid?: boolean;
  /** Show the quick-pick row. */
  presets?: boolean;
  className?: string;
  align?: "start" | "end";
  id?: string;
  onBlur?: () => void;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-invalid"?: boolean;
  "aria-describedby"?: string;
  "aria-required"?: boolean;
}

export const DateTimePicker = forwardRef<HTMLButtonElement, DateTimePickerProps>(function DateTimePicker(
  {
    value,
    onChange,
    clearable,
    disabled,
    placeholder = "Pick a date and time",
    size = "md",
    invalid,
    presets = true,
    className,
    align = "start",
    id,
    onBlur,
    ...aria
  },
  ref,
) {
  const [open, setOpen] = useState(false);
  const date = parseIso(value);
  const [month, setMonth] = useState<Date>(() => date ?? new Date());
  const timeId = useId();

  const change = (next: boolean) => {
    setOpen(next);
    if (next) setMonth(parseIso(value) ?? new Date());
    else onBlur?.();
  };
  const commit = (d: Date) => onChange(d.toISOString());

  const selectDay = (day: Date | undefined) => {
    if (!day) return;
    commit(withTime(day, date?.getHours() ?? 9, date?.getMinutes() ?? 0));
  };
  const setTime = (part: "h" | "m", raw: string) => {
    const n = Number(raw);
    if (!Number.isInteger(n)) return;
    const base = date ?? presetDate("today");
    const h = part === "h" ? Math.min(23, Math.max(0, n)) : base.getHours();
    const m = part === "m" ? Math.min(59, Math.max(0, n)) : base.getMinutes();
    commit(withTime(base, h, m));
  };
  const stepTime = (part: "h" | "m") => (e: KeyboardEvent<HTMLInputElement>) => {
    const dir = e.key === "ArrowUp" ? 1 : e.key === "ArrowDown" ? -1 : 0;
    if (!dir) return;
    e.preventDefault();
    const base = date ?? presetDate("today");
    const span = part === "h" ? 24 : 60;
    const cur = part === "h" ? base.getHours() : base.getMinutes();
    setTime(part, String((cur + dir + span) % span));
  };

  return (
    <div className={cn("relative", className)}>
      <Popover open={open} onOpenChange={change}>
        <PopoverTrigger asChild>
          <button
            ref={ref}
            type="button"
            id={id}
            aria-haspopup="dialog"
            aria-expanded={open}
            aria-label={aria["aria-label"]}
            aria-labelledby={aria["aria-labelledby"]}
            aria-invalid={invalid || aria["aria-invalid"] || undefined}
            aria-describedby={aria["aria-describedby"]}
            aria-required={aria["aria-required"]}
            disabled={disabled}
            className={cn(fieldBase, "inline-flex items-center gap-2 text-left", size === "sm" ? "h-7 px-2 text-[12.5px]" : "h-8", clearable && date && "pr-8")}
          >
            <CalendarClock className="size-4 shrink-0 text-faint" aria-hidden />
            <span className={cn("min-w-0 flex-1 truncate", !date && "text-faint")}>{date ? displayFmt.format(date) : placeholder}</span>
          </button>
        </PopoverTrigger>
        <PopoverContent align={align} className="w-auto p-0" aria-label={aria["aria-label"] ?? "Date and time"}>
          {presets ? (
            <div className="flex flex-wrap gap-1 border-b border-line p-2">
              {PRESETS.map((p) => (
                <Button
                  key={p.id}
                  size="sm"
                  variant="ghost"
                  className="h-6 px-2 text-[12px]"
                  onClick={() => {
                    commit(presetDate(p.id));
                    change(false);
                  }}
                >
                  {p.label}
                </Button>
              ))}
            </div>
          ) : null}
          <div className="p-3">
            <DayPicker
              mode="single"
              selected={date ?? undefined}
              onSelect={selectDay}
              month={month}
              onMonthChange={setMonth}
              showOutsideDays
              weekStartsOn={firstDayOfWeek()}
              classNames={dayPickerClasses}
              formatters={{
                formatCaption: (d) => captionFmt.format(d),
                formatWeekdayName: (d) => weekdayFmt.format(d),
              }}
              components={{
                Chevron: ({ orientation, className: c }) =>
                  orientation === "left" ? <ChevronLeft className={c} aria-hidden /> : <ChevronRight className={c} aria-hidden />,
              }}
            />
          </div>
          <div className="flex items-center justify-between gap-3 border-t border-line px-3 py-2">
            <div className="flex items-center gap-1 text-[13px]">
              <label htmlFor={`${timeId}-h`} className="sr-only">
                Hour
              </label>
              <input
                id={`${timeId}-h`}
                inputMode="numeric"
                value={date ? pad(date.getHours()) : ""}
                placeholder="09"
                maxLength={2}
                onChange={(e) => setTime("h", e.target.value.replace(/\D/g, "") || "0")}
                onKeyDown={stepTime("h")}
                className={cn(fieldBase, "h-7 w-11 px-0 text-center font-mono tabular-nums")}
              />
              <span className="text-muted">:</span>
              <label htmlFor={`${timeId}-m`} className="sr-only">
                Minute
              </label>
              <input
                id={`${timeId}-m`}
                inputMode="numeric"
                value={date ? pad(date.getMinutes()) : ""}
                placeholder="00"
                maxLength={2}
                onChange={(e) => setTime("m", e.target.value.replace(/\D/g, "") || "0")}
                onKeyDown={stepTime("m")}
                className={cn(fieldBase, "h-7 w-11 px-0 text-center font-mono tabular-nums")}
              />
              <span className="ml-1 text-[11.5px] text-faint">24h</span>
            </div>
            <div className="flex items-center gap-1">
              {clearable && date ? (
                <Button
                  size="sm"
                  variant="ghost"
                  onClick={() => {
                    onChange(null);
                    change(false);
                  }}
                >
                  Clear
                </Button>
              ) : null}
              <Button size="sm" variant="primary" onClick={() => change(false)}>
                Done
              </Button>
            </div>
          </div>
        </PopoverContent>
      </Popover>
      {clearable && date && !disabled ? (
        <button
          type="button"
          aria-label="Clear date"
          onClick={() => onChange(null)}
          className="absolute right-2 top-1/2 grid size-5 -translate-y-1/2 place-items-center rounded text-faint hover:bg-surface-2 hover:text-ink"
        >
          <X className="size-3.5" />
        </button>
      ) : null}
    </div>
  );
});

/** react-hook-form binding; the field holds an ISO string or `""`. */
export function FormDateTimePicker<
  TFieldValues extends FieldValues,
  TName extends FieldPath<TFieldValues>,
  TContext = unknown,
  TTransformed extends FieldValues = TFieldValues,
>({
  control,
  name,
  ...props
}: Omit<DateTimePickerProps, "value" | "onChange" | "onBlur"> & { control: Control<TFieldValues, TContext, TTransformed>; name: TName }) {
  return (
    <Controller
      control={control}
      name={name}
      render={({ field, fieldState }) => (
        <DateTimePicker
          {...props}
          ref={field.ref}
          value={typeof field.value === "string" ? field.value : null}
          onChange={(v) => field.onChange(v ?? "")}
          onBlur={field.onBlur}
          disabled={props.disabled ?? field.disabled}
          invalid={props.invalid ?? fieldState.invalid}
        />
      )}
    />
  );
}
