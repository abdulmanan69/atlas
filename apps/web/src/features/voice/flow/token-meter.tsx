import { estimateTokens } from "@/features/voice/flow/validate";
import { cn } from "@/lib/utils";

/** "≈ 420 tokens of 1,500": how much of the prompt budget a text uses, in the warning colour once over. */
export function TokenMeter({ text, budget, what = "prompt", extra = 0, className }: { text: string; budget: number; what?: string; extra?: number; className?: string }) {
  const tokens = estimateTokens(text);
  const over = tokens > budget;
  const fill = Math.min(100, (tokens / budget) * 100);
  return (
    <div className={cn("flex flex-col gap-1", className)} data-over={over || undefined}>
      <div className="flex items-baseline justify-between gap-2 text-[11.5px]">
        <span className={cn("font-mono", over ? "font-semibold text-warn" : "text-muted")}>
          ≈ {tokens.toLocaleString("en-US")} tokens of {budget.toLocaleString("en-US")}
        </span>
        <span className={cn("text-right", over ? "text-warn" : "text-faint")}>
          {over ? `Long ${what} slows every reply` : extra > 0 ? `+ ≈ ${extra.toLocaleString("en-US")} from global rules` : "Shorter replies start sooner"}
        </span>
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-surface-2" role="meter" aria-label={`Estimated ${what} tokens`} aria-valuemin={0} aria-valuemax={budget} aria-valuenow={tokens}>
        <div className={cn("h-full rounded-full transition-[width]", over ? "bg-warn" : fill > 80 ? "bg-warn/60" : "bg-accent/60")} style={{ width: `${fill}%` }} />
      </div>
    </div>
  );
}
