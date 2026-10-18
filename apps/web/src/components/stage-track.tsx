/**
 * Stage track — a segmented meter of the open pipeline. Segments fill up to the lead's
 * current open stage; a won stage fills every segment; a lost stage greys the track and
 * marks where the lead dropped out. It encodes real position, not decoration.
 */
import type { Schemas } from "@/lib/api";
import { cn } from "@/lib/utils";

type Stage = Pick<Schemas["StageOut"], "id" | "key" | "name" | "position" | "category">;

export function StageTrack({
  stages,
  currentStageId,
  showLabel = true,
  size = "sm",
  className,
}: {
  stages: Stage[];
  currentStageId: string;
  showLabel?: boolean;
  size?: "sm" | "md";
  className?: string;
}) {
  const ordered = [...stages].sort((a, b) => a.position - b.position);
  const open = ordered.filter((s) => s.category === "open");
  const current = ordered.find((s) => s.id === currentStageId);
  if (!current) return null;

  const openIndex = open.findIndex((s) => s.id === current.id);
  const lastOpenBefore = open.filter((s) => s.position < current.position).length;
  const filled =
    current.category === "won" ? open.length : current.category === "lost" ? lastOpenBefore : openIndex + 1;

  const fillClass = current.category === "won" ? "bg-success" : current.category === "lost" ? "bg-lost" : "bg-accent";
  const label =
    current.category === "won"
      ? `${current.name} — pipeline complete`
      : current.category === "lost"
        ? `${current.name} — left the pipeline after ${filled} of ${open.length} stages`
        : `${current.name} — stage ${filled} of ${open.length}`;

  return (
    <div className={cn("flex min-w-0 items-center gap-2", className)} role="img" aria-label={label} title={label}>
      <div className="flex shrink-0 gap-[3px]" aria-hidden>
        {open.map((s, i) => (
          <span
            key={s.id}
            className={cn(
              "rounded-[1.5px]",
              size === "sm" ? "h-2.5 w-[5px]" : "h-3.5 w-2",
              i < filled ? fillClass : "bg-line-strong/70",
            )}
          />
        ))}
        {current.category !== "open" ? (
          <span
            className={cn(
              "ml-[2px] rounded-full",
              size === "sm" ? "size-2.5" : "size-3.5",
              current.category === "won" ? "bg-success" : "border-2 border-lost bg-transparent",
            )}
          />
        ) : null}
      </div>
      {showLabel ? (
        <span
          className={cn(
            "truncate text-[12.5px] font-medium",
            current.category === "won" ? "text-success" : current.category === "lost" ? "text-muted" : "text-ink-2",
          )}
        >
          {current.name}
        </span>
      ) : null}
    </div>
  );
}
