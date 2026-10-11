"use client";

import "@xyflow/react/dist/style.css";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Background,
  BackgroundVariant,
  Handle,
  MarkerType,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { AlertTriangle, Check, CircleHelp, Hand, Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { HEADS, type HeadName, type HeadStatus, type RunSnapshot } from "@/lib/run-state";

interface HeadNodeData extends Record<string, unknown> {
  label: string;
  blurb: string;
  status: HeadStatus;
}
type HeadNode = Node<HeadNodeData, "head">;

const STATUS_TEXT: Record<HeadStatus, string> = {
  idle: "Waiting",
  active: "Working",
  done: "Done",
  error: "Hit a problem",
  approval: "Needs your OK",
  question: "Has a question",
};

const STATUS_STYLE: Record<HeadStatus, string> = {
  idle: "border-slate-700 bg-slate-900/80 text-slate-400",
  active: "head-glow border-indigo-400 bg-indigo-950 text-white",
  done: "border-emerald-500/60 bg-emerald-950/60 text-emerald-100",
  error: "border-red-500 bg-red-950/70 text-red-100",
  approval: "border-amber-400 bg-amber-950/70 text-amber-100",
  question: "border-sky-400 bg-sky-950/70 text-sky-100",
};

function StatusIcon({ status }: { status: HeadStatus }) {
  const cls = "size-4 shrink-0";
  switch (status) {
    case "active":
      return <Loader2 className={cn(cls, "animate-spin text-indigo-300 motion-reduce:animate-none")} aria-hidden />;
    case "done":
      return <Check className={cn(cls, "text-emerald-400")} aria-hidden />;
    case "error":
      return <AlertTriangle className={cn(cls, "text-red-400")} aria-hidden />;
    case "approval":
      return <Hand className={cn(cls, "text-amber-300")} aria-hidden />;
    case "question":
      return <CircleHelp className={cn(cls, "text-sky-300")} aria-hidden />;
    default:
      return <span className="size-2 rounded-full bg-slate-600" aria-hidden />;
  }
}

const hidden = { opacity: 0, pointerEvents: "none" as const };

function HeadNodeView({ data }: NodeProps<HeadNode>) {
  return (
    <div
      className={cn(
        "w-[168px] rounded-xl border px-3 py-2.5 transition-colors duration-300",
        STATUS_STYLE[data.status],
      )}
    >
      {(["top", "right", "bottom", "left"] as const).map((side) => {
        const position = { top: Position.Top, right: Position.Right, bottom: Position.Bottom, left: Position.Left }[side];
        return (
          <span key={side}>
            <Handle id={`t-${side}`} type="target" position={position} style={hidden} isConnectable={false} />
            <Handle id={`s-${side}`} type="source" position={position} style={hidden} isConnectable={false} />
          </span>
        );
      })}
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-semibold tracking-tight">{data.label}</span>
        <StatusIcon status={data.status} />
      </div>
      <div className="mt-0.5 text-[11px] leading-tight opacity-70">{data.blurb}</div>
      <div className="mt-1.5 text-[10px] font-medium uppercase tracking-wider opacity-80">
        {STATUS_TEXT[data.status]}
      </div>
    </div>
  );
}

const nodeTypes = { head: HeadNodeView };

const COL = 220;
const ROW = 118;

/** Wide: two rows snaking left→right then right→left. Narrow: one column. */
function layout(compact: boolean): Record<HeadName, { x: number; y: number }> {
  const out = {} as Record<HeadName, { x: number; y: number }>;
  HEADS.forEach((h, i) => {
    if (compact) out[h.id] = { x: 0, y: i * 104 };
    else {
      const row = Math.floor(i / 4);
      const col = i % 4;
      out[h.id] = { x: (row === 0 ? col : 3 - col) * COL, y: row * ROW };
    }
  });
  return out;
}

function buildEdges(snapshot: RunSnapshot, compact: boolean): Edge[] {
  const { heads, activeHead } = snapshot;
  const edges: Edge[] = [];
  const style = (lit: boolean, live: boolean, loop = false): Pick<Edge, "animated" | "style" | "markerEnd"> => {
    const color = live ? "#818cf8" : lit ? "#34d399" : "#475569";
    return {
      animated: live,
      style: { stroke: color, strokeWidth: live ? 2.5 : 1.75, strokeDasharray: loop && !live ? "6 5" : undefined },
      markerEnd: { type: MarkerType.ArrowClosed, color },
    };
  };
  HEADS.slice(0, -1).forEach((h, i) => {
    const next = HEADS[i + 1];
    let sourceHandle = "s-right";
    let targetHandle = "t-left";
    if (compact) {
      sourceHandle = "s-bottom";
      targetHandle = "t-top";
    } else if (i === 3) {
      sourceHandle = "s-bottom";
      targetHandle = "t-top";
    } else if (i > 3) {
      sourceHandle = "s-left";
      targetHandle = "t-right";
    }
    const live = activeHead === next.id && heads[h.id] !== "idle";
    edges.push({
      id: `${h.id}-${next.id}`,
      source: h.id,
      target: next.id,
      sourceHandle,
      targetHandle,
      type: "smoothstep",
      ...style(heads[h.id] === "done", live),
    });
  });
  // The improvement loop: a verified fix sends the run back to the Runner.
  const loopLive = activeHead === "runner" && snapshot.iteration > 1;
  edges.push({
    id: "verifier-runner",
    source: "verifier",
    target: "runner",
    sourceHandle: compact ? "s-right" : "s-bottom",
    targetHandle: compact ? "t-right" : "t-bottom",
    type: "smoothstep",
    label: "retest",
    labelStyle: { fill: "#94a3b8", fontSize: 11 },
    labelBgStyle: { fill: "#020617" },
    ...style(snapshot.iteration > 1, loopLive, true),
  });
  return edges;
}

export function HeadsGraph({ snapshot }: { snapshot: RunSnapshot }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [compact, setCompact] = useState(false);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const update = () => setCompact(el.clientWidth < 640);
    update();
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  const nodes = useMemo<HeadNode[]>(() => {
    const pos = layout(compact);
    return HEADS.map((h) => ({
      id: h.id,
      type: "head",
      position: pos[h.id],
      draggable: false,
      selectable: false,
      data: { label: h.label, blurb: h.blurb, status: snapshot.heads[h.id] },
    }));
  }, [snapshot.heads, compact]);

  const edges = useMemo(() => buildEdges(snapshot, compact), [snapshot, compact]);

  return (
    <div
      ref={wrapRef}
      className={cn("w-full", compact ? "h-[800px]" : "h-[340px]")}
      role="img"
      aria-label="Progress through the eight stages of the run"
    >
      <ReactFlow
        key={compact ? "compact" : "wide"}
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.12 }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
        panOnDrag={false}
        zoomOnScroll={false}
        zoomOnPinch={false}
        zoomOnDoubleClick={false}
        preventScrolling={false}
        proOptions={{ hideAttribution: true }}
        colorMode="dark"
        className="!bg-transparent"
      >
        <Background variant={BackgroundVariant.Dots} gap={22} size={1.2} color="#1e293b" />
      </ReactFlow>
    </div>
  );
}
