/** A path between nodes: the curve, a name pill at its middle, and the condition on hover. */
import { BaseEdge, EdgeLabelRenderer, getBezierPath, type Edge, type EdgeProps } from "@xyflow/react";
import { CircleAlert, TriangleAlert } from "lucide-react";

import { Tooltip } from "@/components/ui/primitives";
import type { FlowPath } from "@/features/voice/flow/types";
import { cn } from "@/lib/utils";

export type CanvasEdgeData = { path: FlowPath; errors: number; warnings: number; onSelect: () => void };
export type CanvasEdge = Edge<CanvasEdgeData, "path">;

export function PathEdge({ id, sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition, selected, data, markerEnd }: EdgeProps<CanvasEdge>) {
  const [edgePath, labelX, labelY] = getBezierPath({ sourceX, sourceY, targetX, targetY, sourcePosition, targetPosition });
  if (!data) return <BaseEdge id={id} path={edgePath} markerEnd={markerEnd} />;
  const { path, errors, warnings } = data;
  const when = path.when.trim();
  return (
    <>
      <BaseEdge id={id} path={edgePath} markerEnd={markerEnd} />
      <EdgeLabelRenderer>
        <div
          className="nodrag nopan pointer-events-auto absolute"
          style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
        >
          <Tooltip
            content={
              <span className="block max-w-56">
                <span className="block text-[11px] uppercase tracking-wider opacity-70">When</span>
                {when || <span className="italic">Not described yet</span>}
                {path.say_while_moving?.trim() ? <span className="mt-1 block opacity-80">Says: “{path.say_while_moving.trim()}”</span> : null}
              </span>
            }
          >
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                data.onSelect();
              }}
              aria-label={`Path ${path.name}${when ? `: ${when}` : ""}`}
              className={cn(
                "inline-flex h-6 max-w-44 items-center gap-1 rounded-full border bg-surface px-2.5 text-[11.5px] font-medium shadow-card transition-colors",
                selected ? "border-accent text-accent" : errors ? "border-danger/70 text-danger" : "border-line-strong text-ink-2 hover:border-accent hover:text-accent",
              )}
            >
              {errors ? <CircleAlert className="size-3 shrink-0" aria-hidden /> : warnings ? <TriangleAlert className="size-3 shrink-0 text-warn" aria-hidden /> : null}
              <span className="truncate">{path.name}</span>
            </button>
          </Tooltip>
        </div>
      </EdgeLabelRenderer>
    </>
  );
}
