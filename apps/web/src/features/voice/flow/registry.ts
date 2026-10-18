/** Node and edge renderers keyed by type, memoised once for React Flow. */
import { memo } from "react";

import { PathEdge } from "@/features/voice/flow/edges";
import { NodeCard } from "@/features/voice/flow/nodes";

const node = memo(NodeCard);
export const nodeTypes = { start: node, step: node, end: node };
export const edgeTypes = { path: memo(PathEdge) };
