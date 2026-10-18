/**
 * The React Flow surface. The editor reducer owns the flow; this component mirrors it
 * into React Flow's node/edge state (keeping measured sizes and in-progress drags) and
 * turns gestures back into reducer actions, so every change is one undoable step.
 */
import {
  applyEdgeChanges,
  applyNodeChanges,
  Background,
  BackgroundVariant,
  MarkerType,
  MiniMap,
  ReactFlow,
  useReactFlow,
  type Connection,
  type EdgeChange,
  type IsValidConnection,
  type NodeChange,
  type OnSelectionChangeParams,
} from "@xyflow/react";
import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";

import "@xyflow/react/dist/style.css";
import "@/features/voice/flow/flow.css";

import type { CanvasEdge } from "@/features/voice/flow/edges";
import { canConnect, type EditorAction, type EditorState } from "@/features/voice/flow/model";
import { reducedMotion } from "@/features/voice/flow/motion";
import type { CanvasNode } from "@/features/voice/flow/nodes";
import { edgeTypes, nodeTypes } from "@/features/voice/flow/registry";
import { KIND_LABEL, type FlowIssue, type IssueTarget, type Viewport } from "@/features/voice/flow/types";
import { targetKey } from "@/features/voice/flow/validate";

export interface FocusRequest {
  target: IssueTarget;
  id: string;
  nonce: number;
}

export interface FlowCanvasProps {
  /** Scopes the remembered viewport to one agent. */
  agentId: string;
  state: EditorState;
  dispatch: (action: EditorAction) => void;
  errors: Map<string, FlowIssue[]>;
  warnings: Map<string, FlowIssue[]>;
  snap: boolean;
  readOnly: boolean;
  focus?: FocusRequest | null;
}

/** Where the person left the canvas, per agent and browser. A convenience: never saved. */
function readViewport(agentId: string): Viewport | null {
  try {
    const raw = localStorage.getItem(`atlas.flow.viewport.${agentId}`);
    const v = raw ? (JSON.parse(raw) as Partial<Viewport>) : null;
    return v && typeof v.x === "number" && typeof v.y === "number" && typeof v.zoom === "number" ? { x: v.x, y: v.y, zoom: v.zoom } : null;
  } catch {
    return null;
  }
}
function writeViewport(agentId: string, v: Viewport) {
  try {
    localStorage.setItem(`atlas.flow.viewport.${agentId}`, JSON.stringify(v));
  } catch {
    // Private mode or blocked storage: the canvas simply fits to view next time.
  }
}

const isTyping = (target: EventTarget | null) => {
  const el = target as HTMLElement | null;
  return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);
};

export function FlowCanvas({ agentId, state, dispatch, errors, warnings, snap, readOnly, focus }: FlowCanvasProps) {
  const { flow, selection } = state;
  const [remembered] = useState(() => readViewport(agentId));
  const [nodes, setNodes] = useState<CanvasNode[]>([]);
  const [edges, setEdges] = useState<CanvasEdge[]>([]);
  const { fitView } = useReactFlow();
  const dispatchRef = useRef(dispatch);
  dispatchRef.current = dispatch;

  useEffect(() => {
    setNodes((prev) => {
      const prevById = new Map(prev.map((n) => [n.id, n]));
      return flow.nodes.map((n) => {
        const old = prevById.get(n.id);
        const key = targetKey("node", n.id);
        return {
          ...old,
          id: n.id,
          type: n.kind,
          position: old?.dragging ? old.position : n.position,
          data: { node: n, errors: errors.get(key)?.length ?? 0, warnings: warnings.get(key)?.length ?? 0 },
          selected: selection?.kind === "node" && selection.id === n.id,
          draggable: !readOnly,
          connectable: !readOnly,
          deletable: false,
          ariaLabel: `${KIND_LABEL[n.kind]} ${n.name}`,
        };
      });
    });
  }, [flow.nodes, selection, errors, warnings, readOnly]);

  useEffect(() => {
    setEdges(
      flow.paths.map((p) => {
        const key = targetKey("path", p.id);
        const errorCount = errors.get(key)?.length ?? 0;
        return {
          id: p.id,
          source: p.from_node,
          target: p.to_node,
          type: "path" as const,
          data: { path: p, errors: errorCount, warnings: warnings.get(key)?.length ?? 0, onSelect: () => dispatchRef.current({ type: "select", selection: { kind: "path", id: p.id } }) },
          selected: selection?.kind === "path" && selection.id === p.id,
          className: errorCount ? "has-error" : undefined,
          deletable: false,
          focusable: true,
          ariaLabel: `Path ${p.name}`,
          markerEnd: { type: MarkerType.ArrowClosed, width: 18, height: 18 },
        };
      }),
    );
  }, [flow.paths, selection, errors, warnings]);

  useEffect(() => {
    if (!focus) return;
    const ids = focus.target === "node" ? [focus.id] : focus.target === "path" ? flow.paths.filter((p) => p.id === focus.id).flatMap((p) => [p.from_node, p.to_node]) : [];
    if (!ids.length) return;
    const t = setTimeout(() => void fitView({ nodes: ids.map((id) => ({ id })), padding: 0.6, maxZoom: 1, duration: reducedMotion() ? 0 : 250 }), 0);
    return () => clearTimeout(t);
  }, [focus, fitView, flow.paths]);

  const onNodesChange = useCallback((changes: NodeChange<CanvasNode>[]) => setNodes((ns) => applyNodeChanges(changes, ns)), []);
  const onEdgesChange = useCallback((changes: EdgeChange<CanvasEdge>[]) => setEdges((es) => applyEdgeChanges(changes, es)), []);

  const onSelectionChange = useCallback(({ nodes: picked, edges: pickedEdges }: OnSelectionChangeParams<CanvasNode, CanvasEdge>) => {
    const node = picked[0];
    const edge = pickedEdges[0];
    dispatchRef.current({ type: "select", selection: node ? { kind: "node", id: node.id } : edge ? { kind: "path", id: edge.id } : null });
  }, []);

  const onConnect = useCallback((c: Connection) => {
    if (c.source && c.target) dispatchRef.current({ type: "connect", from: c.source, to: c.target });
  }, []);
  const isValidConnection = useCallback<IsValidConnection<CanvasEdge>>((c) => !!c.source && !!c.target && canConnect(flow, c.source, c.target), [flow]);

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (isTyping(e.target)) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && e.key.toLowerCase() === "z") {
      e.preventDefault();
      dispatch({ type: e.shiftKey ? "redo" : "undo" });
    } else if (mod && e.key.toLowerCase() === "y") {
      e.preventDefault();
      dispatch({ type: "redo" });
    } else if ((e.key === "Delete" || e.key === "Backspace") && !readOnly) {
      e.preventDefault();
      dispatch({ type: "delete" });
    } else if (e.key === "Escape") {
      dispatch({ type: "select", selection: null });
    }
  };

  return (
    <div className="atlas-flow h-full w-full" onKeyDown={onKeyDown} data-testid="flow-canvas">
      <ReactFlow<CanvasNode, CanvasEdge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onSelectionChange={onSelectionChange}
        onConnect={onConnect}
        isValidConnection={isValidConnection}
        onNodeDragStop={(_, __, dragged) => dispatch({ type: "move", positions: Object.fromEntries(dragged.map((n) => [n.id, n.position])) })}
        onMoveEnd={(_, viewport) => writeViewport(agentId, viewport)}
        defaultViewport={remembered ?? undefined}
        fitView={!remembered}
        fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
        minZoom={0.15}
        maxZoom={1.75}
        snapToGrid={snap}
        snapGrid={[20, 20]}
        deleteKeyCode={null}
        selectionKeyCode={null}
        multiSelectionKeyCode={null}
        nodesDraggable={!readOnly}
        nodesConnectable={!readOnly}
        elementsSelectable
        nodesFocusable
        edgesFocusable
        connectionRadius={28}
        defaultEdgeOptions={{ type: "path" }}
        proOptions={{ hideAttribution: false }}
        aria-label="Flow canvas"
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1.2} />
        <MiniMap pannable zoomable position="bottom-right" nodeClassName={(n) => `kind-${n.type ?? "step"}`} aria-label="Overview" />
      </ReactFlow>
    </div>
  );
}
