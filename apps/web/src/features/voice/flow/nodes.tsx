/**
 * Canvas nodes. Each shows what matters at a glance: kind, name, a prompt excerpt, what it
 * captures or can use, and whether validation found something. The three-segment meter
 * echoes the pipeline stage track: Start fills one, a Step two, an End all three.
 */
import { Handle, Position, type Node, type NodeProps } from "@xyflow/react";
import { BookOpen, CircleAlert, ListChecks, Quote, TriangleAlert, Zap } from "lucide-react";

import { KIND_LABEL, type FlowNode, type NodeKind } from "@/features/voice/flow/types";
import { cn } from "@/lib/utils";

export type CanvasNodeData = { node: FlowNode; errors: number; warnings: number };
export type CanvasNode = Node<CanvasNodeData, NodeKind>;

export const NODE_WIDTH = 280;

const TONE: Record<NodeKind, { strip: string; eyebrow: string; fill: string; surface: string }> = {
  start: { strip: "bg-accent", eyebrow: "text-accent", fill: "bg-accent", surface: "bg-surface" },
  step: { strip: "bg-line-strong", eyebrow: "text-muted", fill: "bg-accent", surface: "bg-surface" },
  end: { strip: "bg-success", eyebrow: "text-success", fill: "bg-success", surface: "bg-success-soft/40" },
};

function KindMeter({ kind }: { kind: NodeKind }) {
  const filled = kind === "start" ? 1 : kind === "step" ? 2 : 3;
  return (
    <span className="flex gap-[3px]" aria-hidden>
      {[0, 1, 2].map((i) => (
        <span key={i} className={cn("h-2.5 w-[5px] rounded-[1.5px]", i < filled ? TONE[kind].fill : "bg-line-strong/70")} />
      ))}
    </span>
  );
}

function Count({ icon, value, label }: { icon: React.ReactNode; value: number; label: string }) {
  if (!value) return null;
  return (
    <span className="inline-flex items-center gap-1 rounded-[4px] border border-line bg-surface px-1.5 py-px text-[11px] font-medium text-ink-2 [&_svg]:size-3 [&_svg]:text-muted" title={`${value} ${label}`}>
      {icon}
      {value}
    </span>
  );
}

export function NodeCard({ data, selected }: NodeProps<CanvasNode>) {
  const { node, errors, warnings } = data;
  const tone = TONE[node.kind];
  const prompt = node.prompt.trim();
  return (
    <div
      className={cn(
        "relative flex overflow-hidden rounded-[10px] border shadow-card transition-colors",
        tone.surface,
        selected ? "border-accent ring-2 ring-accent/30" : errors ? "border-danger/70" : "border-line-strong hover:border-faint",
      )}
      style={{ width: NODE_WIDTH }}
    >
      <span className={cn("w-1 shrink-0", tone.strip)} aria-hidden />
      <div className="min-w-0 flex-1 px-3 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <span className={cn("eyebrow", tone.eyebrow)}>{KIND_LABEL[node.kind]}</span>
          <span className="flex items-center gap-2">
            {errors ? (
              <span className="inline-flex items-center gap-0.5 text-[11px] font-semibold text-danger" title={`${errors} ${errors === 1 ? "issue" : "issues"}`}>
                <CircleAlert className="size-3.5" aria-hidden />
                {errors}
              </span>
            ) : warnings ? (
              <span className="inline-flex items-center gap-0.5 text-[11px] font-semibold text-warn" title={`${warnings} ${warnings === 1 ? "warning" : "warnings"}`}>
                <TriangleAlert className="size-3.5" aria-hidden />
                {warnings}
              </span>
            ) : null}
            <KindMeter kind={node.kind} />
          </span>
        </div>
        <p className="mt-1 truncate text-[13.5px] font-semibold text-ink">{node.name}</p>
        {node.kind === "start" ? (
          <p className="mt-1 flex items-start gap-1 text-[11.5px] text-ink-2">
            <Quote className="mt-0.5 size-3 shrink-0 text-faint" aria-hidden />
            <span className="truncate">{(node.greeting ?? "").trim() || <span className="italic text-faint">No greeting yet</span>}</span>
          </p>
        ) : null}
        <p className={cn("mt-1 line-clamp-2 text-[12px] leading-snug", prompt ? "text-muted" : "italic text-faint")}>{prompt || "Nothing written yet"}</p>
        {node.capture_fields.length || node.action_ids.length || node.knowledge_ids.length ? (
          <div className="mt-2 flex flex-wrap gap-1">
            <Count icon={<ListChecks />} value={node.capture_fields.length} label="captured details" />
            {node.kind !== "end" ? <Count icon={<Zap />} value={node.action_ids.length} label="actions" /> : null}
            {node.kind !== "end" ? <Count icon={<BookOpen />} value={node.knowledge_ids.length} label="documents" /> : null}
          </div>
        ) : null}
      </div>
      {node.kind !== "start" ? <Handle type="target" position={Position.Left} aria-label="Incoming paths" /> : null}
      {node.kind !== "end" ? <Handle type="source" position={Position.Right} aria-label="Drag to add a path" /> : null}
    </div>
  );
}
