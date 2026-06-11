"use client";

/**
 * BlackboardGraph — live entry/ref graph visualization (doc 08 §3).
 *
 * Renders board entries as nodes with type glyphs, agent dots, salience
 * weighting, and confidence bars. Refs between entries render as typed
 * edges. Extends DAGVisualizer patterns using @xyflow/react.
 */

import React, { useMemo, useCallback, useState } from "react";
import {
  ReactFlow,
  Background,
  Controls,
  MiniMap,
  type Node,
  type Edge,
  type NodeMouseHandler,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { authorColor, ENTRY_TYPE_ICONS } from "@/lib/design-tokens";
import { getActiveAdapter } from "@/lib/variants";
import type { BoardEntry } from "@/hooks/useTaskStream";
import {
  Target, Paperclip, ListTree, Lightbulb, AlertTriangle,
  MessageSquareReply, GitMerge, CheckCircle2, FileCode2, Network,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

// ── Icon lookup ───────────────────────────────────────────────────────

const ICON_MAP: Record<string, LucideIcon> = {
  Target, Paperclip, ListTree, Lightbulb, AlertTriangle,
  MessageSquareReply, GitMerge, CheckCircle2, FileCode2,
};

function getIcon(entryType: string): LucideIcon {
  const iconName = ENTRY_TYPE_ICONS[entryType];
  return (iconName ? ICON_MAP[iconName] : null) ?? Lightbulb;
}

// ── Layout helpers ────────────────────────────────────────────────────

const NODE_W = 220;
const NODE_H = 64;
const GAP_X = 280;
const GAP_Y = 100;

function layoutNodes(
  entries: BoardEntry[],
  removedIds: Set<string>,
): Node[] {
  // Compute depth from refs (reverse topological)
  const depthMap = new Map<string, number>();
  const entryMap = new Map(entries.map((e) => [e.id, e]));

  function getDepth(id: string): number {
    if (depthMap.has(id)) return depthMap.get(id)!;
    const entry = entryMap.get(id);
    if (!entry || entry.refs.length === 0) {
      depthMap.set(id, 0);
      return 0;
    }
    const maxParent = Math.max(
      ...entry.refs
        .filter((r) => entryMap.has(r))
        .map((r) => getDepth(r)),
      -1,
    );
    const d = maxParent + 1;
    depthMap.set(id, d);
    return d;
  }

  entries.forEach((e) => getDepth(e.id));

  // Group by depth for Y positioning
  const byDepth = new Map<number, BoardEntry[]>();
  for (const entry of entries) {
    const d = depthMap.get(entry.id) ?? 0;
    if (!byDepth.has(d)) byDepth.set(d, []);
    byDepth.get(d)!.push(entry);
  }

  const nodes: Node[] = [];
  for (const [depth, group] of byDepth) {
    group.forEach((entry, idx) => {
      const isRemoved = removedIds.has(entry.id);
      const isSolution = entry.type === "solution";
      // Salience → opacity (0.6–1.0) and scale (1.0–1.06)
      const opacity = 0.6 + Math.min(entry.salience, 1) * 0.4;
      const scale = 1.0 + Math.min(entry.salience, 1) * 0.06;

      nodes.push({
        id: entry.id,
        position: { x: depth * GAP_X, y: idx * GAP_Y },
        data: { entry, isRemoved, isSolution },
        style: {
          width: NODE_W,
          height: NODE_H,
          opacity: isRemoved ? 0.2 : opacity,
          transform: `scale(${isRemoved ? 0.95 : scale})`,
          transition: "opacity 2s ease, transform 300ms ease",
        },
        type: "bbEntry",
      });
    });
  }

  return nodes;
}

function buildEdges(
  entries: BoardEntry[],
  variant: string,
): Edge[] {
  const adapter = getActiveAdapter(variant);
  const edgeSpecs = adapter?.edgeSpecs ?? [];
  const edges: Edge[] = [];
  const entryIds = new Set(entries.map((e) => e.id));

  for (const entry of entries) {
    for (const ref of entry.refs) {
      if (!entryIds.has(ref)) continue;
      const spec = edgeSpecs.find((s) => s.refType === "supports") ?? edgeSpecs[0];
      edges.push({
        id: `${ref}-${entry.id}`,
        source: ref,
        target: entry.id,
        animated: spec?.animated ?? false,
        style: {
          stroke: "var(--border-default)",
          strokeDasharray: spec?.stroke === "dashed" ? "5 5" : spec?.stroke === "dotted" ? "2 4" : undefined,
        },
        label: spec?.label,
      });
    }
  }

  return edges;
}

// ── Custom Node Component ─────────────────────────────────────────────

function BlackboardEntryNode({ data }: { data: { entry: BoardEntry; isRemoved: boolean; isSolution: boolean } }) {
  const { entry, isRemoved, isSolution } = data;
  const Icon = getIcon(entry.type);
  const color = authorColor(entry.author);

  return (
    <div
      className={`bb-graph-node ${isRemoved ? "entry-remove" : "entry-appear"} ${isSolution ? "success-bloom" : ""}`}
      style={{
        background: "var(--surface-overlay)",
        borderRadius: "var(--radius-md)",
        padding: "var(--space-2) var(--space-3)",
        display: "flex",
        alignItems: "center",
        gap: "var(--space-2)",
        border: `1px solid ${isRemoved ? "var(--status-error)" : "var(--border-default)"}`,
        height: "100%",
        position: "relative",
        overflow: "hidden",
      }}
    >
      {/* Type glyph */}
      <Icon size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />

      {/* Agent dot */}
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: "var(--radius-full)",
          background: color,
          flexShrink: 0,
        }}
      />

      {/* Content */}
      <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 1 }}>
        <span
          style={{
            fontSize: "var(--text-xs)",
            fontWeight: "var(--weight-semibold)",
            color: "var(--text-primary)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
          }}
        >
          {entry.title || entry.type}
        </span>
        <span
          style={{
            fontSize: "var(--text-xs)",
            color: "var(--text-tertiary)",
            fontFamily: "var(--font-mono)",
          }}
        >
          {entry.author}
        </span>
      </div>

      {/* Confidence bar */}
      <div
        style={{
          position: "absolute",
          bottom: 0,
          left: 0,
          height: 2,
          width: `${Math.min(entry.confidence * 100, 100)}%`,
          background: color,
          borderRadius: "0 0 var(--radius-md) var(--radius-md)",
          transition: "width 500ms ease-out",
        }}
      />
    </div>
  );
}

const nodeTypes = { bbEntry: BlackboardEntryNode };

// ── Props ─────────────────────────────────────────────────────────────

interface BlackboardGraphProps {
  entries: BoardEntry[];
  removedEntryIds: string[];
  variant?: string;
  onNodeClick?: (entryId: string) => void;
}

// ── Component ─────────────────────────────────────────────────────────

export function BlackboardGraph({
  entries,
  removedEntryIds,
  variant = "traditional",
  onNodeClick,
}: BlackboardGraphProps) {
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const removedSet = useMemo(() => new Set(removedEntryIds), [removedEntryIds]);
  const nodes = useMemo(() => layoutNodes(entries, removedSet), [entries, removedSet]);
  const edges = useMemo(() => buildEdges(entries, variant), [entries, variant]);

  const handleNodeClick: NodeMouseHandler = useCallback(
    (_event, node) => {
      setSelectedNode(node.id);
      onNodeClick?.(node.id);
    },
    [onNodeClick],
  );

  if (entries.length === 0) {
    return (
      <div
        className="bb-graph-empty"
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          height: "100%",
          gap: "var(--space-3)",
          color: "var(--text-tertiary)",
        }}
      >
        <Network size={32} />
        <span style={{ fontSize: "var(--text-sm)" }}>
          Waiting for board entries…
        </span>
      </div>
    );
  }

  return (
    <div style={{ width: "100%", height: "100%", minHeight: 400 }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={handleNodeClick}
        fitView
        proOptions={{ hideAttribution: true }}
        style={{ background: "var(--surface-base)" }}
      >
        <Background color="var(--border-subtle)" gap={20} />
        <Controls
          style={{ background: "var(--surface-overlay)", borderRadius: "var(--radius-md)" }}
        />
        <MiniMap
          nodeColor={(n) => {
            if (n.id === selectedNode) return "var(--accent-primary)";
            const entry = (n.data as { entry: BoardEntry }).entry;
            return authorColor(entry.author);
          }}
          style={{
            background: "var(--surface-raised)",
            borderRadius: "var(--radius-md)",
          }}
        />
      </ReactFlow>
    </div>
  );
}

export default BlackboardGraph;
