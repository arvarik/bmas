"use client";

/**
 * TurnGraph — execution graph for the traditional (blackboard) variant.
 *
 * ── Design rationale ──────────────────────────────────────────────────
 * The real execution model is a *cyclic* multi-agent blackboard loop: each
 * round the Control Unit (CU) picks 1–N agents to activate (with a rationale),
 * they run concurrently as "turns", and routing can loop back to earlier
 * agents (e.g. critiqued authors). A naive actor→actor graph therefore has
 * cycles and a tangle of back-edges.
 *
 * We break the cycle by laying the graph out on a **time axis**: rounds are
 * columns (left → right = time), and each agent gets a horizontal **swimlane**
 * (row). A turn is a node at (round column, agent lane). An agent re-activated
 * in a later round simply appears again further right in the *same* lane — the
 * repetition is visible without a single back-edge.
 *
 * The decision/handoff flow is made explicit with a **coordinator spine**: the
 * top lane holds one CU decision node per round. Edges fan out from each CU
 * decision to the agents it activated that round ("activates"), and a thin
 * spine links consecutive decisions ("then"). Hovering/clicking any node opens
 * a detail panel with who / what / when / why (actor, model, node, timestamps,
 * duration, cost, and the routing rationale).
 *
 * Everything is interactive (pan/zoom/minimap) and consistent with the design
 * system (design-tokens colors, CSS variables, lucide icons).
 */

import React, { useEffect, useMemo, useState, useCallback } from "react";
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
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { authorColor } from "@/lib/design-tokens";
import type { TurnRecord, CoordinatorNarration, RosterEntry } from "@/hooks/useTaskStream";
import {
  Activity,
  CheckCircle,
  XCircle,
  Clock,
  Compass,
  X,
  Cpu,
  Server,
  Timer,
  Coins,
  GitBranch,
} from "lucide-react";

// ── Layout constants ────────────────────────────────────────────────────

const HEADER_W = 150; // left gutter for lane labels
const COL_W = 250; // horizontal spacing between round columns
const NODE_W = 188;
const TURN_H = 60;
const COORD_H = 96;
const COORD_LANE_H = 150; // vertical band reserved for the coordinator lane
const LANE_PAD = 34; // vertical padding around a lane's nodes
const STACK_GAP = 10; // gap between stacked turns in the same (round, actor) cell
const TOP = 8;

// ── Role meanings (mirror daemon CONSTANT_ROLE_DESCRIPTIONS) ──────────────

const ROLE_MEANING: Record<string, string> = {
  planner: "Decomposes the objective into actionable sub-goals and plans.",
  critic: "Identifies errors, hallucinations, and weak reasoning in findings.",
  conflict_resolver:
    "Detects contradictions between entries and mediates resolution.",
  cleaner: "Removes redundant or obsolete entries to keep the board focused.",
  decider: "Judges whether the board is sufficient and posts the final solution.",
  expert: "Task-specific domain expert; investigates and posts findings.",
  universal: "Roleless agent (stigmergic variant).",
  control_unit: "Coordinator — selects which agents act each round, and why.",
  operator: "Human operator directive injected into the board.",
};

function roleMeaning(role: string, actorAbility?: string): string {
  // For experts, prefer the AG-generated ability description over the generic fallback.
  if (role === "expert" && actorAbility) return actorAbility;
  return ROLE_MEANING[role] ?? "Agent activated by the coordinator.";
}

// ── Helpers ───────────────────────────────────────────────────────────────

function baseRole(actor: string): string {
  return actor.includes(".") ? actor.split(".")[0] : actor;
}

/** "expert.valuation_analyst" → "Valuation Analyst" */
function prettyActor(actor: string): string {
  const tail = actor.includes(".") ? actor.split(".").slice(1).join(".") : actor;
  return tail
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function fmtTime(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function fmtDuration(start?: string, end?: string): string {
  if (!start || !end) return "—";
  const ms = new Date(end).getTime() - new Date(start).getTime();
  if (!Number.isFinite(ms) || ms < 0) return "—";
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
}

function statusVisuals(status: string) {
  const isRunning = status === "running" || status === "active";
  const isCompleted = status === "completed";
  const isFailed = status === "failed" || status === "timeout";
  const isDeclined = status === "declined";
  const Icon = isRunning
    ? Activity
    : isCompleted
    ? CheckCircle
    : isFailed
    ? XCircle
    : Clock;
  const tone = isRunning
    ? "hsl(142, 71%, 45%)"
    : isCompleted
    ? null // use actor color
    : isFailed
    ? "hsl(0, 72%, 51%)"
    : "hsl(38, 92%, 50%)"; // declined / other → amber
  return { Icon, tone, isRunning, isCompleted, isFailed, isDeclined };
}

// ── View models ─────────────────────────────────────────────────────────

interface TurnVM {
  id: string;
  actor: string;
  role: string;
  round: number;
  status: string;
  model?: string;
  node?: string;
  startedAt?: string;
  endedAt?: string;
  tokensIn?: number;
  tokensOut?: number;
  costUsd?: number;
  rationale?: string | null;
  phase?: string;
  live: boolean;
}

interface CoordVM {
  id: string;
  round: number;
  phase?: string;
  rationale?: string | null;
  source?: string;
  selected: string[];
}

type Selection =
  | { kind: "turn"; data: TurnVM }
  | { kind: "coord"; data: CoordVM }
  | null;

// ── Custom nodes ──────────────────────────────────────────────────────────

interface LaneData {
  actor: string;
  role: string;
  [key: string]: unknown;
}

function LaneLabelNode({ data }: { data: LaneData }) {
  // Use full actor string for color so each expert gets a distinct hue.
  const color = authorColor(data.actor);
  return (
    <div
      style={{
        width: HEADER_W - 16,
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "6px 8px",
        borderRight: `2px solid ${color}55`,
      }}
      title={data.actor}
    >
      <span
        style={{
          width: 10,
          height: 10,
          borderRadius: 3,
          background: color,
          flexShrink: 0,
        }}
      />
      <span
        style={{
          fontSize: 12,
          fontWeight: 600,
          color: "var(--text-secondary)",
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
          textTransform: "capitalize",
        }}
      >
        {data.actor.includes(".") ? prettyActor(data.actor) : data.role.replace(/_/g, " ")}
      </span>
    </div>
  );
}

interface CoordNodeData extends Record<string, unknown> {
  round: number;
  phase?: string;
  rationale?: string | null;
  count: number;
  selected: boolean;
}

function CoordinatorNode({ data }: { data: CoordNodeData }) {
  const accent = "hsl(217, 91%, 60%)";
  return (
    <div
      style={{
        background: data.selected ? "hsl(217 60% 16%)" : "hsl(222 44% 11%)",
        border: `2px solid ${data.selected ? accent : "hsl(217 50% 38%)"}`,
        borderRadius: 10,
        padding: "8px 12px",
        width: NODE_W,
        minHeight: COORD_H,
        boxShadow: data.selected
          ? `0 0 0 3px ${accent}33`
          : "0 2px 8px hsl(220 47% 5% / 0.4)",
        fontFamily: "var(--font-sans)",
        display: "flex",
        flexDirection: "column",
        gap: 5,
        cursor: "pointer",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Compass size={14} style={{ color: accent, flexShrink: 0 }} />
        <span style={{ fontSize: 12, fontWeight: 700, color: "var(--text-primary)" }}>
          Round {data.round}
        </span>
        <span style={{ flex: 1 }} />
        {data.phase && (
          <span
            style={{
              fontSize: 9,
              letterSpacing: 0.4,
              textTransform: "uppercase",
              color: accent,
              background: `${accent}1f`,
              padding: "1px 6px",
              borderRadius: 4,
              fontWeight: 600,
            }}
          >
            {data.phase}
          </span>
        )}
      </div>
      <div
        style={{
          fontSize: 10.5,
          lineHeight: 1.35,
          color: "var(--text-secondary)",
          display: "-webkit-box",
          WebkitLineClamp: 3,
          WebkitBoxOrient: "vertical",
          overflow: "hidden",
        }}
      >
        {data.rationale || "Routing rationale not recorded for this task."}
      </div>
      <div style={{ fontSize: 9.5, color: "var(--text-tertiary)" }}>
        activated {data.count} agent{data.count !== 1 ? "s" : ""}
      </div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
      <Handle id="down" type="source" position={Position.Bottom} style={{ opacity: 0 }} />
    </div>
  );
}

interface TurnNodeData extends Record<string, unknown> {
  actor: string;
  role: string;
  round: number;
  model?: string;
  status: string;
  duration: string;
  selected: boolean;
}

function TurnNode({ data }: { data: TurnNodeData }) {
  const color = authorColor(data.role);
  const { Icon, tone, isRunning } = statusVisuals(data.status);
  const borderColor = tone ?? color;
  return (
    <div
      style={{
        background: isRunning ? "hsl(142 40% 10%)" : "hsl(222 36% 13%)",
        border: `2px solid ${data.selected ? "hsl(210 20% 96%)" : borderColor}`,
        borderRadius: 9,
        padding: "8px 11px",
        width: NODE_W,
        minHeight: TURN_H,
        boxShadow: data.selected
          ? `0 0 0 3px ${color}44`
          : isRunning
          ? `0 0 14px ${borderColor}44`
          : "0 2px 6px hsl(220 47% 5% / 0.4)",
        transition: "border-color 200ms, box-shadow 200ms",
        fontFamily: "var(--font-sans)",
        display: "flex",
        flexDirection: "column",
        gap: 3,
        cursor: "pointer",
      }}
    >
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <Handle id="top" type="target" position={Position.Top} style={{ opacity: 0 }} />
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          style={{
            width: 8,
            height: 8,
            borderRadius: "50%",
            background: color,
            flexShrink: 0,
          }}
        />
        <span
          style={{
            fontSize: 12,
            fontWeight: 600,
            color: "var(--text-primary)",
            flex: 1,
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            textTransform: "capitalize",
          }}
        >
          {data.actor.includes(".") ? prettyActor(data.actor) : data.role.replace(/_/g, " ")}
        </span>
        <Icon size={12} style={{ color: tone ?? color, flexShrink: 0 }} />
      </div>
      <div style={{ display: "flex", gap: 5, alignItems: "center", flexWrap: "wrap" }}>
        <span
          style={{
            fontSize: 9.5,
            background: "hsl(220 36% 20%)",
            color: "var(--text-tertiary)",
            padding: "1px 5px",
            borderRadius: 4,
            fontFamily: "var(--font-mono)",
          }}
        >
          R{data.round}
        </span>
        {data.model && (
          <span
            style={{
              fontSize: 9.5,
              background: `${color}18`,
              color,
              padding: "1px 5px",
              borderRadius: 4,
              maxWidth: 96,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {data.model.split("/").pop() ?? data.model}
          </span>
        )}
        {data.duration !== "—" && (
          <span style={{ fontSize: 9.5, color: "var(--text-tertiary)" }}>
            {data.duration}
          </span>
        )}
      </div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  );
}

const nodeTypes = {
  laneLabel: LaneLabelNode,
  coordinator: CoordinatorNode,
  turnNode: TurnNode,
};

// ── Graph builder ──────────────────────────────────────────────────────────

interface BuildResult {
  nodes: Node[];
  edges: Edge[];
  turnVMs: Map<string, TurnVM>;
  coordVMs: Map<string, CoordVM>;
}

function buildGraph(
  turns: TurnRecord[],
  narrations: CoordinatorNarration[],
  selectedId: string | null,
): BuildResult {
  const turnVMs = new Map<string, TurnVM>();
  const coordVMs = new Map<string, CoordVM>();
  const nodes: Node[] = [];
  const edges: Edge[] = [];

  if (turns.length === 0) {
    return { nodes, edges, turnVMs, coordVMs };
  }

  // Normalize to view models
  const vms: TurnVM[] = turns.map((t) => {
    const actor = t.actor || t.role || "unknown";
    return {
      id: t.turn_id,
      actor,
      role: t.role || baseRole(actor),
      round: t.round_no ?? 0,
      status: t.status,
      model: t.model,
      node: t.node,
      startedAt: t.started_at,
      endedAt: t.ended_at,
      tokensIn: t.tokens_in,
      tokensOut: t.tokens_out,
      costUsd: t.cost_usd,
      rationale: t.rationale,
      phase: t.phase,
      live: t.status === "running" || t.status === "active",
    };
  });

  // Rounds (columns), sorted
  const rounds = [...new Set(vms.map((v) => v.round))].sort((a, b) => a - b);
  const roundIndex = new Map(rounds.map((r, i) => [r, i]));

  // Lanes (rows) — each UNIQUE actor gets its own lane.
  // For experts (actor = "expert.foo_bar"), this means each expert slug
  // appears as its own dedicated swim-lane rather than all collapsing
  // into a single shared "expert" row.
  const firstSeen = new Map<string, number>();
  vms.forEach((v, i) => {
    if (!firstSeen.has(v.actor)) firstSeen.set(v.actor, i);
  });
  const lanes = [...firstSeen.keys()].sort(
    (a, b) => (firstSeen.get(a) ?? 0) - (firstSeen.get(b) ?? 0),
  );

  // Per (round, actor) cell → list of turns (for stacking)
  const cell = new Map<string, TurnVM[]>();
  for (const v of vms) {
    const key = `${v.round}::${v.actor}`;
    if (!cell.has(key)) cell.set(key, []);
    cell.get(key)!.push(v);
  }

  // Max stack height per lane → variable lane heights
  const laneMaxStack = new Map<string, number>();
  for (const lane of lanes) {
    let max = 1;
    for (const r of rounds) {
      const c = cell.get(`${r}::${lane}`);
      if (c && c.length > max) max = c.length;
    }
    laneMaxStack.set(lane, max);
  }

  // Accumulate lane Y positions
  const laneY = new Map<string, number>();
  let y = TOP + COORD_LANE_H;
  for (const lane of lanes) {
    laneY.set(lane, y);
    const h = TURN_H + (laneMaxStack.get(lane)! - 1) * (TURN_H + STACK_GAP) + LANE_PAD;
    y += h;
  }

  const xFor = (round: number) =>
    HEADER_W + (roundIndex.get(round) ?? 0) * COL_W;

  // Lane label nodes (left gutter)
  for (const lane of lanes) {
    nodes.push({
      id: `lane-${lane}`,
      type: "laneLabel",
      position: { x: 4, y: laneY.get(lane)! },
      data: { actor: lane, role: baseRole(lane) } as LaneData,
      draggable: false,
      selectable: false,
      connectable: false,
    });
  }

  // Coordinator decision nodes (top lane), one per round
  let prevCoordId: string | null = null;
  for (const r of rounds) {
    const roundTurns = vms.filter((v) => v.round === r);
    const rationale =
      roundTurns.find((v) => v.rationale)?.rationale ??
      narrations.find((n) => n.round === r)?.rationale ??
      null;
    const phase =
      roundTurns.find((v) => v.phase && v.phase !== "completed" && v.phase !== "active")
        ?.phase ?? roundTurns[0]?.phase;
    const narration = narrations.find((n) => n.round === r);
    const selectedActors = [...new Set(roundTurns.map((v) => v.actor))];
    const coordId = `coord-${r}`;

    coordVMs.set(coordId, {
      id: coordId,
      round: r,
      phase,
      rationale,
      source: narration?.source,
      selected: selectedActors,
    });

    nodes.push({
      id: coordId,
      type: "coordinator",
      position: { x: xFor(r), y: TOP },
      data: {
        round: r,
        phase,
        rationale,
        count: roundTurns.length,
        selected: selectedId === coordId,
      } as CoordNodeData,
    });

    // Spine: previous decision → this decision
    if (prevCoordId) {
      edges.push({
        id: `spine-${prevCoordId}-${coordId}`,
        source: prevCoordId,
        target: coordId,
        type: "smoothstep",
        style: { stroke: "hsl(217 40% 40%)", strokeWidth: 1.5, strokeDasharray: "4 4" },
        markerEnd: { type: MarkerType.ArrowClosed, color: "hsl(217 40% 40%)", width: 14, height: 14 },
        label: "then",
        labelStyle: { fill: "var(--text-tertiary)", fontSize: 9 },
        labelBgStyle: { fill: "hsl(222 44% 9%)" },
        labelBgPadding: [3, 1],
      });
    }
    prevCoordId = coordId;

    // Activation fan-out: decision → each turn this round
    for (const [ci, key] of [...new Set(roundTurns.map((v) => `${v.round}::${v.actor}`))].entries()) {
      const stack = cell.get(key)!;
      stack.forEach((turn, si) => {
        const color = authorColor(turn.role);
        edges.push({
          id: `act-${coordId}-${turn.id}`,
          source: coordId,
          target: turn.id,
          targetHandle: "top",
          type: "smoothstep",
          animated: turn.live,
          style: { stroke: color, strokeWidth: 1.75, opacity: 0.85 },
          markerEnd: { type: MarkerType.ArrowClosed, color, width: 14, height: 14 },
        });
        void ci;
        void si;
      });
    }
  }

  // Turn nodes
  for (const [key, stack] of cell.entries()) {
    const [roundStr, actor] = key.split("::");
    const round = Number(roundStr);
    const baseY = laneY.get(actor)!;
    stack.forEach((turn, si) => {
      turnVMs.set(turn.id, turn);
      nodes.push({
        id: turn.id,
        type: "turnNode",
        position: { x: xFor(round), y: baseY + si * (TURN_H + STACK_GAP) },
        data: {
          actor: turn.actor,
          role: turn.role,
          round: turn.round,
          model: turn.model,
          status: turn.status,
          duration: fmtDuration(turn.startedAt, turn.endedAt),
          selected: selectedId === turn.id,
        } as TurnNodeData,
      });
    });
  }

  return { nodes, edges, turnVMs, coordVMs };
}

// ── Detail panel ──────────────────────────────────────────────────────────

function DetailRow({ icon: Icon, label, value }: { icon: typeof Cpu; label: string; value: React.ReactNode }) {
  return (
    <div style={{ display: "flex", gap: 8, alignItems: "flex-start" }}>
      <Icon size={13} style={{ color: "var(--text-tertiary)", marginTop: 2, flexShrink: 0 }} />
      <div style={{ display: "flex", flexDirection: "column", minWidth: 0, flex: 1 }}>
        <span style={{ fontSize: 10, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.3 }}>
          {label}
        </span>
        <span style={{ fontSize: 12.5, color: "var(--text-primary)", wordBreak: "break-word" }}>
          {value}
        </span>
      </div>
    </div>
  );
}

function DetailPanel({ selection, roster, onClose }: { selection: Selection; roster: RosterEntry[]; onClose: () => void }) {
  // Look up ability for expert actors from the AG-generated roster.
  // Hook must be called before any conditional returns (React hooks rules).
  const actorAbility: string | undefined = useMemo(() => {
    if (!selection || selection.kind !== "turn" || selection.data.role !== "expert") return undefined;
    return roster.find((r) => r.actor === selection.data.actor)?.ability;
  }, [selection, roster]);

  if (!selection) return null;

  const headerColor =
    selection.kind === "turn"
      ? authorColor(selection.data.actor)
      : "hsl(217, 91%, 60%)";

  return (
    <div
      style={{
        position: "absolute",
        top: 12,
        right: 12,
        bottom: 12,
        width: 300,
        background: "var(--surface-raised)",
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-lg)",
        boxShadow: "var(--shadow-lg)",
        zIndex: 10,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "12px 14px",
          borderBottom: "1px solid var(--border-default)",
          borderLeft: `3px solid ${headerColor}`,
        }}
      >
        {selection.kind === "coord" ? (
          <Compass size={15} style={{ color: headerColor }} />
        ) : (
          <span style={{ width: 10, height: 10, borderRadius: "50%", background: headerColor }} />
        )}
        <span style={{ fontSize: 13, fontWeight: 700, color: "var(--text-primary)", flex: 1 }}>
          {selection.kind === "coord"
            ? `Round ${selection.data.round} · Routing`
            : selection.data.actor.includes(".")
            ? prettyActor(selection.data.actor)
            : selection.data.role.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase())}
        </span>
        <button
          onClick={onClose}
          aria-label="Close details"
          style={{
            background: "transparent",
            border: "none",
            color: "var(--text-tertiary)",
            cursor: "pointer",
            display: "flex",
            padding: 2,
          }}
        >
          <X size={15} />
        </button>
      </div>

      <div style={{ padding: 14, display: "flex", flexDirection: "column", gap: 12, overflowY: "auto" }}>
        {selection.kind === "turn" ? (
          <>
            <DetailRow icon={GitBranch} label="Role" value={
              <span style={{ textTransform: "capitalize" }}>{selection.data.role.replace(/_/g, " ")}</span>
            } />
            <p style={{ fontSize: 11.5, color: "var(--text-secondary)", lineHeight: 1.45, margin: 0 }}>
              {roleMeaning(selection.data.role, actorAbility)}
            </p>
            <DetailRow icon={Activity} label="Status" value={
              <span style={{ textTransform: "capitalize" }}>{selection.data.status}</span>
            } />
            <DetailRow icon={Compass} label="Round / Phase" value={
              `Round ${selection.data.round}${selection.data.phase ? ` · ${selection.data.phase}` : ""}`
            } />
            {selection.data.model && (
              <DetailRow icon={Cpu} label="Model" value={selection.data.model} />
            )}
            {selection.data.node && (
              <DetailRow icon={Server} label="Node" value={selection.data.node} />
            )}
            <DetailRow icon={Clock} label="Started" value={fmtTime(selection.data.startedAt)} />
            <DetailRow icon={Timer} label="Duration" value={fmtDuration(selection.data.startedAt, selection.data.endedAt)} />
            {(selection.data.tokensIn != null || selection.data.tokensOut != null) && (
              <DetailRow icon={Coins} label="Tokens (in / out)" value={
                `${selection.data.tokensIn ?? 0} / ${selection.data.tokensOut ?? 0}`
              } />
            )}
            {selection.data.costUsd != null && (
              <DetailRow icon={Coins} label="Cost" value={`$${selection.data.costUsd.toFixed(6)}`} />
            )}
            <div style={{ height: 1, background: "var(--border-default)" }} />
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <span style={{ fontSize: 10, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.3 }}>
                Why activated
              </span>
              <p style={{ fontSize: 12, color: "var(--text-primary)", lineHeight: 1.5, margin: 0 }}>
                {selection.data.rationale || "Routing rationale not recorded for this turn."}
              </p>
            </div>
          </>
        ) : (
          <>
            <p style={{ fontSize: 11.5, color: "var(--text-secondary)", lineHeight: 1.45, margin: 0 }}>
              {roleMeaning("control_unit")}
            </p>
            {selection.data.phase && (
              <DetailRow icon={Compass} label="Phase" value={selection.data.phase} />
            )}
            {selection.data.source && (
              <DetailRow icon={Cpu} label="Decision source" value={
                selection.data.source === "llm" ? "LLM (Control Unit)" : "Deterministic policy"
              } />
            )}
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <span style={{ fontSize: 10, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.3 }}>
                Activated agents
              </span>
              <div style={{ display: "flex", flexWrap: "wrap", gap: 5 }}>
                {selection.data.selected.map((a) => (
                  <span
                    key={a}
                    style={{
                      fontSize: 11,
                      padding: "2px 8px",
                      borderRadius: 6,
                      background: `${authorColor(baseRole(a))}22`,
                      color: authorColor(baseRole(a)),
                      textTransform: "capitalize",
                    }}
                  >
                    {a.includes(".") ? prettyActor(a) : a.replace(/_/g, " ")}
                  </span>
                ))}
              </div>
            </div>
            <div style={{ height: 1, background: "var(--border-default)" }} />
            <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
              <span style={{ fontSize: 10, color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: 0.3 }}>
                Routing rationale
              </span>
              <p style={{ fontSize: 12, color: "var(--text-primary)", lineHeight: 1.5, margin: 0 }}>
                {selection.data.rationale || "Routing rationale not recorded for this task."}
              </p>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Legend ──────────────────────────────────────────────────────────────

function Legend() {
  return (
    <div
      style={{
        position: "absolute",
        bottom: 12,
        left: 12,
        zIndex: 5,
        background: "hsl(222 44% 9% / 0.9)",
        border: "1px solid var(--border-default)",
        borderRadius: "var(--radius-md)",
        padding: "8px 10px",
        display: "flex",
        flexDirection: "column",
        gap: 5,
        fontSize: 10.5,
        color: "var(--text-secondary)",
        backdropFilter: "blur(4px)",
        pointerEvents: "none",
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Compass size={11} style={{ color: "hsl(217,91%,60%)" }} />
        <span>Coordinator decision (per round)</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="hsl(150 45% 50%)" strokeWidth="2" /></svg>
        <span>activates → agent turn</span>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <svg width="22" height="8"><line x1="0" y1="4" x2="22" y2="4" stroke="hsl(217 40% 40%)" strokeWidth="1.5" strokeDasharray="3 3" /></svg>
        <span>then → next round</span>
      </div>
      <div style={{ color: "var(--text-tertiary)" }}>
        Columns = rounds (time →) · rows = agents
      </div>
    </div>
  );
}

// ── Empty state ────────────────────────────────────────────────────────────

function EmptyGraph({ isLive }: { isLive: boolean }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        height: "100%",
        gap: 12,
        color: "var(--text-tertiary)",
        padding: 32,
      }}
    >
      <Activity size={28} />
      <span style={{ fontSize: "var(--text-sm)", textAlign: "center" }}>
        {isLive ? "Waiting for agent turns to start…" : "No turn data recorded for this task."}
      </span>
    </div>
  );
}

// ── Main component ──────────────────────────────────────────────────────────

interface TurnGraphProps {
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  isLive: boolean;
  narrations?: CoordinatorNarration[];
  /** AG-generated expert roster for the detail panel ability descriptions. */
  roster?: RosterEntry[];
}

export function TurnGraph({ activeTurns, completedTurns, isLive, narrations = [], roster = [] }: TurnGraphProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const allTurns = useMemo(() => {
    const byId = new Map<string, TurnRecord>();
    for (const t of [...completedTurns, ...activeTurns]) {
      if (t.turn_id) byId.set(t.turn_id, t);
    }
    return [...byId.values()].sort(
      (a, b) =>
        (a.round_no ?? 0) - (b.round_no ?? 0) ||
        (a.started_at ?? "").localeCompare(b.started_at ?? ""),
    );
  }, [completedTurns, activeTurns]);

  const built = useMemo(
    () => buildGraph(allTurns, narrations, selectedId),
    [allTurns, narrations, selectedId],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(built.nodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(built.edges);

  useEffect(() => {
    setNodes(built.nodes);
    setEdges(built.edges);
  }, [built.nodes, built.edges, setNodes, setEdges]);

  const selection: Selection = useMemo(() => {
    if (!selectedId) return null;
    const t = built.turnVMs.get(selectedId);
    if (t) return { kind: "turn", data: t };
    const c = built.coordVMs.get(selectedId);
    if (c) return { kind: "coord", data: c };
    return null;
  }, [selectedId, built.turnVMs, built.coordVMs]);

  const onNodeClick = useCallback((_: React.MouseEvent, node: Node) => {
    if (node.type === "laneLabel") return;
    setSelectedId((prev) => (prev === node.id ? null : node.id));
  }, []);

  const onPaneClick = useCallback(() => setSelectedId(null), []);

  if (allTurns.length === 0) {
    return <EmptyGraph isLive={isLive} />;
  }

  return (
    <div style={{ width: "100%", height: "100%", position: "relative" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        onPaneClick={onPaneClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.2}
        maxZoom={2}
        colorMode="dark"
        proOptions={{ hideAttribution: true }}
        nodesConnectable={false}
        nodesDraggable
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="hsl(220 36% 20%)" />
        <Controls showInteractive={false} />
        <MiniMap
          nodeColor={(n) => {
            if (n.type === "coordinator") return "hsl(217,91%,60%)";
            // Use full actor string for color so each expert has a distinct hue.
            const d = n.data as { actor?: string; role?: string };
            return authorColor(d.actor ?? d.role ?? "unknown");
          }}
          maskColor="hsl(222 47% 6% / 0.85)"
          style={{ background: "hsl(222 36% 10%)" }}
          pannable
          zoomable
        />
      </ReactFlow>
      <Legend />
      <DetailPanel selection={selection} roster={roster} onClose={() => setSelectedId(null)} />
    </div>
  );
}

export default TurnGraph;
