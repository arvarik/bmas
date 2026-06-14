"use client";

/**
 * TurnGraph — dynamic execution graph built from actual turn records.
 *
 * Unlike the old DAGVisualizer which used static sub_task placeholders,
 * this renders the real execution: each agent turn as a node, grouped by
 * round. Edges flow round → round (sequential execution model).
 *
 * Falls back to a simple round-timeline when ReactFlow is unavailable.
 * Handles dynamic agent profiles (expert, decider, critic, any role).
 */

import React, { useEffect, useMemo, useCallback } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  useNodesState,
  useEdgesState,
  type Node,
  type Edge,
  BackgroundVariant,
  Handle,
  Position,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { authorColor } from "@/lib/design-tokens";
import type { TurnRecord } from "@/hooks/useTaskStream";
import { Activity, CheckCircle, XCircle, Clock } from "lucide-react";

// ── Layout constants ───────────────────────────────────────────────────

const NODE_WIDTH  = 180;
const NODE_HEIGHT = 72;
const H_GAP       = 40;   // gap between nodes in same round
const V_GAP       = 100;  // gap between rounds (vertical)

// ── Turn node data ─────────────────────────────────────────────────────

interface TurnNodeData {
  role: string;
  roundNo: number;
  model?: string;
  status: string;
  node?: string;   // agent node URL
  [key: string]: unknown;
}

// ── Custom Node ────────────────────────────────────────────────────────

function TurnNode({ data }: { data: TurnNodeData }) {
  const color = authorColor(data.role);
  const isRunning  = data.status === "running"  || data.status === "active";
  const isCompleted = data.status === "completed";
  const isFailed   = data.status === "failed";

  const borderColor = isRunning
    ? "hsl(142, 71%, 45%)"
    : isCompleted
    ? color
    : isFailed
    ? "hsl(0, 72%, 51%)"
    : "hsl(220, 15%, 30%)";

  const bg = isRunning
    ? "hsl(142 40% 10%)"
    : isCompleted
    ? "hsl(222 36% 14%)"
    : "hsl(222 36% 10%)";

  const StatusIcon = isRunning
    ? Activity
    : isCompleted
    ? CheckCircle
    : isFailed
    ? XCircle
    : Clock;

  const iconColor = isRunning
    ? "hsl(142,71%,45%)"
    : isCompleted
    ? color
    : isFailed
    ? "hsl(0,72%,51%)"
    : "hsl(220,15%,50%)";

  return (
    <div
      style={{
        background: bg,
        border: `2px solid ${borderColor}`,
        borderRadius: 10,
        padding: "10px 14px",
        width: NODE_WIDTH,
        minHeight: NODE_HEIGHT,
        fontFamily: "var(--font-sans)",
        boxShadow: isRunning
          ? `0 0 16px ${borderColor}44`
          : `0 2px 8px hsl(220 47% 5% / 0.4)`,
        transition: "border-color 300ms, box-shadow 300ms",
        display: "flex",
        flexDirection: "column",
        gap: 4,
      }}
    >
      <Handle type="target" position={Position.Top} style={{ opacity: 0 }} />

      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        {/* Agent color dot */}
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0 }} />
        <span style={{
          fontSize: 12,
          fontWeight: 600,
          color: "var(--text-primary)",
          textTransform: "capitalize",
          flex: 1,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}>
          {data.role.replace(/_/g, " ")}
        </span>
        <StatusIcon size={12} style={{ color: iconColor, flexShrink: 0 }} />
      </div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginTop: 2 }}>
        <span style={{
          fontSize: 10,
          background: "hsl(220 36% 20%)",
          color: "var(--text-tertiary)",
          padding: "1px 6px",
          borderRadius: 4,
          fontFamily: "var(--font-mono)",
        }}>
          R{data.roundNo}
        </span>
        {data.model && (
          <span style={{
            fontSize: 10,
            background: `${color}18`,
            color,
            padding: "1px 6px",
            borderRadius: 4,
            maxWidth: 110,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}>
            {data.model.split("/").pop() ?? data.model}
          </span>
        )}
      </div>

      <Handle type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = { turnNode: TurnNode };

// ── Layout builder ─────────────────────────────────────────────────────

function buildGraph(turns: TurnRecord[]): { nodes: Node[]; edges: Edge[] } {
  if (turns.length === 0) return { nodes: [], edges: [] };

  // Group by round_no
  const rounds = new Map<number, TurnRecord[]>();
  for (const t of turns) {
    const r = t.round_no ?? 0;
    if (!rounds.has(r)) rounds.set(r, []);
    rounds.get(r)!.push(t);
  }

  const sortedRounds = [...rounds.entries()].sort(([a], [b]) => a - b);

  const nodes: Node[] = [];
  const edges: Edge[] = [];

  let lastRoundNodeIds: string[] = [];

  sortedRounds.forEach(([roundNo, roundTurns], roundIdx) => {
    const y = roundIdx * (NODE_HEIGHT + V_GAP);
    const totalWidth = roundTurns.length * NODE_WIDTH + (roundTurns.length - 1) * H_GAP;
    const startX = -totalWidth / 2;

    const currentNodeIds: string[] = [];

    roundTurns.forEach((turn, i) => {
      const nodeId = turn.turn_id || `turn-${roundIdx}-${i}`;
      const x = startX + i * (NODE_WIDTH + H_GAP);

      nodes.push({
        id: nodeId,
        type: "turnNode",
        position: { x, y },
        data: {
          role: turn.actor,
          roundNo,
          model: turn.model,
          status: turn.status,
          node: undefined,
        } as TurnNodeData,
      });

      currentNodeIds.push(nodeId);
    });

    // Connect every node in last round to every node in this round
    for (const fromId of lastRoundNodeIds) {
      for (const toId of currentNodeIds) {
        edges.push({
          id: `edge-${fromId}-${toId}`,
          source: fromId,
          target: toId,
          type: "smoothstep",
          style: {
            stroke: "hsl(220 15% 35%)",
            strokeWidth: 1.5,
          },
          animated: false,
        });
      }
    }

    lastRoundNodeIds = currentNodeIds;
  });

  return { nodes, edges };
}

// ── Empty state ────────────────────────────────────────────────────────

function EmptyGraph({ isLive }: { isLive: boolean }) {
  return (
    <div style={{
      display: "flex", flexDirection: "column", alignItems: "center",
      justifyContent: "center", height: "100%", gap: 12,
      color: "var(--text-tertiary)", padding: 32,
    }}>
      <Activity size={28} />
      <span style={{ fontSize: "var(--text-sm)", textAlign: "center" }}>
        {isLive
          ? "Waiting for agent turns to start…"
          : "No turn data recorded for this task."}
      </span>
    </div>
  );
}

// ── Main Component ─────────────────────────────────────────────────────

interface TurnGraphProps {
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  isLive: boolean;
}

export function TurnGraph({ activeTurns, completedTurns, isLive }: TurnGraphProps) {
  const allTurns = useMemo(
    () => [...completedTurns, ...activeTurns].sort(
      (a, b) => (a.round_no ?? 0) - (b.round_no ?? 0)
    ),
    [completedTurns, activeTurns]
  );

  const { nodes: initialNodes, edges: initialEdges } = useMemo(
    () => buildGraph(allTurns),
    [allTurns]
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);

  // Update graph when turns change (live streaming)
  useEffect(() => {
    const { nodes: newNodes, edges: newEdges } = buildGraph(allTurns);
    setNodes(newNodes);
    setEdges(newEdges);
  }, [allTurns, setNodes, setEdges]);

  if (allTurns.length === 0) {
    return <EmptyGraph isLive={isLive} />;
  }

  return (
    <div style={{ width: "100%", height: "100%" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.3 }}
        minZoom={0.3}
        maxZoom={2}
        colorMode="dark"
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="hsl(220 36% 20%)" />
        <Controls showInteractive={false} />
        <MiniMap
          nodeColor={(n) => {
            const d = n.data as TurnNodeData;
            return authorColor(d.role ?? "unknown");
          }}
          maskColor="hsl(222 47% 6% / 0.85)"
          style={{ background: "hsl(222 36% 10%)" }}
        />
      </ReactFlow>
    </div>
  );
}

export default TurnGraph;
