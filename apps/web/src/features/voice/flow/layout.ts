/** "Tidy up": a left-to-right dagre layout with the spacing the runtime's own editor uses. */
import { Graph, layout } from "@dagrejs/dagre";

import type { FlowDefinition } from "@/features/voice/flow/types";

export const LAYOUT = { width: 350, height: 120, nodesep: 400, ranksep: 300 } as const;

export function tidyUp(flow: FlowDefinition): FlowDefinition {
  if (flow.nodes.length === 0) return flow;
  const g = new Graph();
  g.setGraph({ rankdir: "LR", nodesep: LAYOUT.nodesep, ranksep: LAYOUT.ranksep });
  g.setDefaultEdgeLabel(() => ({}));
  // Start first and Ends last so dagre's rank order reads naturally.
  const order = [...flow.nodes].sort((a, b) => rank(a.kind) - rank(b.kind));
  for (const n of order) g.setNode(n.id, { width: LAYOUT.width, height: LAYOUT.height });
  const ids = new Set(flow.nodes.map((n) => n.id));
  for (const p of flow.paths) if (ids.has(p.from_node) && ids.has(p.to_node) && p.from_node !== p.to_node) g.setEdge(p.from_node, p.to_node);
  layout(g);
  const nodes = flow.nodes.map((n) => {
    const placed = g.node(n.id) as { x?: number; y?: number } | undefined;
    if (!placed || placed.x === undefined || placed.y === undefined) return n;
    // dagre reports centres; the canvas positions by top-left corner.
    return { ...n, position: { x: Math.round(placed.x - LAYOUT.width / 2), y: Math.round(placed.y - LAYOUT.height / 2) } };
  });
  return { ...flow, nodes };
}

const rank = (kind: "start" | "step" | "end") => (kind === "start" ? 0 : kind === "end" ? 2 : 1);
