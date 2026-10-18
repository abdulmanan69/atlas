import { Select, SelectItem } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { DAY_LABELS, END_TIME_OPTIONS, hoursError, TIME_OPTIONS, WEEKDAYS, type HoursDraft } from "@/features/campaigns/campaigns-model";
import { cn } from "@/lib/utils";

/**
 * One row per weekday: a switch for whether calls may be placed that day and a from/to
 * window in the campaign's time zone. The error, if any, is announced below the grid.
 */
export function CallingHoursEditor({ value, onChange, disabled, error }: { value: HoursDraft; onChange: (next: HoursDraft) => void; disabled?: boolean; error?: string | null }) {
  const message = error ?? hoursError(value);
  const set = (day: keyof HoursDraft, patch: Partial<HoursDraft[keyof HoursDraft]>) => onChange({ ...value, [day]: { ...value[day], ...patch } });

  return (
    <div className="rounded-md border border-line">
      <ul className="divide-y divide-line">
        {WEEKDAYS.map((d) => {
          const w = value[d];
          return (
            <li key={d} className={cn("grid grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-3 px-3 py-2", !w.enabled && "text-muted")}>
              <Switch aria-label={`Calls on ${DAY_LABELS[d]}`} size="sm" checked={w.enabled} disabled={disabled} onCheckedChange={(v) => set(d, { enabled: v })} />
              <span className={cn("text-[13px]", w.enabled ? "font-medium text-ink" : "text-muted")}>{DAY_LABELS[d]}</span>
              <div className="flex items-center gap-1.5">
                <Select aria-label={`${DAY_LABELS[d]} from`} size="sm" value={w.from} onValueChange={(v) => set(d, { from: v })} disabled={disabled || !w.enabled} className="w-24 font-mono">
                  {TIME_OPTIONS.map((t) => (
                    <SelectItem key={t} value={t} className="font-mono">
                      {t}
                    </SelectItem>
                  ))}
                </Select>
                <span className="text-[12px] text-faint">to</span>
                <Select aria-label={`${DAY_LABELS[d]} to`} size="sm" value={w.to} onValueChange={(v) => set(d, { to: v })} disabled={disabled || !w.enabled} className="w-24 font-mono">
                  {END_TIME_OPTIONS.map((t) => (
                    <SelectItem key={t} value={t} className="font-mono">
                      {t}
                    </SelectItem>
                  ))}
                </Select>
              </div>
            </li>
          );
        })}
      </ul>
      {message ? (
        <p role="alert" className="border-t border-line px-3 py-2 text-[12px] text-danger">
          {message}
        </p>
      ) : null}
    </div>
  );
}
