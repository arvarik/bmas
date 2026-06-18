"use client";

/**
 * ProcessFlowGraph — directed round-by-round execution graph for the task overview.
 *
 * Replaces the old linear "Triage → Expert → Critic → Decider" linked-list with
 * a proper graph that shows:
 *   - Which agents ran in each round (grouped by phase)
 *   - Forward edges  (round N → round N+1)
 *   - Cycle-back edges (Debate returning to refine an earlier expert group)
 *   - Phase colour-coding
 *   - Coordinator rationale on click
 *
 * Built in plain SVG + HTML — no heavy graph library needed.
 */

import React, {
  useMemo, useState, useRef, useEffect, useCallback, forwardRef,
} from "react";
import { authorColor } from "@/lib/design-tokens";
import type { TurnRecord, CoordinatorNarration } from "@/hooks/useTaskStream";
import {
  Check, Activity, XCircle, Clock, Info,
  GitMerge, Cpu, Users, RotateCcw,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface RoundNode {
  round: number;
  phase: string;
  actors: string[];
  status: "completed" | "running" | "failed" | "pending";
  turnCount: number;
  rationale?: string;
  durationMs?: number;
}

interface GraphEdge {
  from: number;   // index into nodes[]
  to: number;     // index into nodes[]
  isCycle: boolean;
  label?: string;
}

interface GraphLayout {
  nodes: RoundNode[];
  edges: GraphEdge[];
  cycleTargets: Set<number>;  // node indices that are targets of cycle edges
}

type NodePos = { x: number; y: number; w: number; h: number };

// ── Phase colours ─────────────────────────────────────────────────────────────

const PHASE_COLORS: Record<string, string> = {
  discovery:   "hsl(217, 91%, 60%)",
  planning:    "hsl(265, 60%, 66%)",
  debate:      "hsl(350, 72%, 62%)",
  critique:    "hsl(350, 72%, 62%)",
  rebuttal:    "hsl(199, 80%, 58%)",
  convergence: "hsl(142, 71%, 48%)",
  resolution:  "hsl(142, 71%, 48%)",
  cleanup:     "hsl(220, 12%, 55%)",
  triage:      "hsl(38, 92%, 56%)",
};

function phaseColor(phase: string): string {
  return PHASE_COLORS[(phase ?? "").toLowerCase()] ?? "hsl(220, 15%, 50%)";
}

// ── Display helpers ────────────────────────────────────────────────────────────

const ROLE_DISPLAY: Record<string, string> = {
  planner: "Planner", critic: "Critic",
  conflict_resolver: "Resolver", cleaner: "Cleaner",
  decider: "Decider", control_unit: "Coordinator",
};

function prettyActor(actor: string): string {
  if (actor.includes(".")) {
    return actor.split(".").slice(1).join(".")
      .replace(/_/g, " ")
      .replace(/\b\w/g, (c) => c.toUpperCase());
  }
  return (ROLE_DISPLAY[actor] ??
    actor.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()));
}

function prettyPhase(phase: string): string {
  if (!phase) return "Unknown";
  return phase.charAt(0).toUpperCase() + phase.slice(1);
}

function fmtMs(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = ms / 1000;
  if (s < 60) return `${s.toFixed(1)}s`;
  return `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`;
}

// ── Graph builder ──────────────────────────────────────────────────────────────

export function buildFlowGraph(
  turns: TurnRecord[],
  narrations: CoordinatorNarration[],
): GraphLayout {
  if (turns.length === 0) return { nodes: [], edges: [], cycleTargets: new Set() };

  // Sort by round then start time
  const sorted = [...turns].sort(
    (a, b) => (a.round_no ?? 0) - (b.round_no ?? 0)
      || (a.started_at ?? "").localeCompare(b.started_at ?? ""),
  );

  // Group by round
  const byRound = new Map<number, TurnRecord[]>();
  for (const t of sorted) {
    const r = t.round_no ?? 0;
    if (!byRound.has(r)) byRound.set(r, []);
    byRound.get(r)!.push(t);
  }

  const narByRound = new Map(narrations.map((n) => [n.round, n.rationale ?? ""]));
  const roundKeys  = [...byRound.keys()].sort((a, b) => a - b);

  const nodes: RoundNode[] = roundKeys.map((r) => {
    const rTurns = byRound.get(r)!;

    // Unique actors in order
    const seen = new Set<string>();
    const actors: string[] = [];
    for (const t of rTurns) {
      if (t.actor && !seen.has(t.actor)) { seen.add(t.actor); actors.push(t.actor); }
    }

    // Dominant phase
    const phaseCount: Record<string, number> = {};
    for (const t of rTurns) {
      const p = (t.phase ?? "unknown").toLowerCase();
      phaseCount[p] = (phaseCount[p] ?? 0) + 1;
    }
    const phase = Object.entries(phaseCount).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "unknown";

    const anyFailed  = rTurns.some((t) => t.status === "failed");
    const anyActive  = rTurns.some((t) => t.status === "active" || t.status === "running");

    const starts = rTurns.map((t) => t.started_at).filter(Boolean).map((s) => +new Date(s!));
    const ends   = rTurns.map((t) => t.ended_at).filter(Boolean).map((s) => +new Date(s!));
    const durationMs = starts.length && ends.length
      ? Math.max(...ends) - Math.min(...starts) : undefined;

    const rationale =
      narByRound.get(r) || rTurns.find((t) => t.rationale)?.rationale || undefined;

    return {
      round: r, phase, actors,
      status: anyFailed ? "failed" : anyActive ? "running" : "completed",
      turnCount: rTurns.length, rationale, durationMs,
    };
  });

  // Build edges
  const edges: GraphEdge[] = [];
  const cycleTargets = new Set<number>();

  for (let i = 1; i < nodes.length; i++) {
    // Always a forward edge from prev → curr
    edges.push({ from: i - 1, to: i, isCycle: false });

    // Cycle detection: current is a debate round that revisits actors from an earlier round
    const curr = nodes[i];
    if (curr.phase === "debate" || curr.phase === "rebuttal") {
      for (let j = 0; j < i - 1; j++) {
        const earlier = nodes[j];
        const shared = curr.actors.filter((a) => earlier.actors.includes(a)).length;
        const ratio  = shared / Math.max(curr.actors.length, earlier.actors.length, 1);
        if (ratio >= 0.35) {
          cycleTargets.add(j);
          const already = edges.some((e) => e.from === i && e.to === j && e.isCycle);
          if (!already) edges.push({ from: i, to: j, isCycle: true, label: "refines" });
          break;
        }
      }
    }
  }

  return { nodes, edges, cycleTargets };
}

// ── SVG edge canvas ────────────────────────────────────────────────────────────

function EdgeCanvas({
  edges,
  pos,
  svgW,
  svgH,
}: {
  edges: GraphEdge[];
  pos: Map<number, NodePos>;
  svgW: number;
  svgH: number;
}) {
  if (pos.size === 0 || svgW === 0) return null;

  const paths: React.ReactNode[] = [];

  for (const edge of edges) {
    const f = pos.get(edge.from);
    const t = pos.get(edge.to);
    if (!f || !t) continue;

    if (!edge.isCycle) {
      // Straight bezier: right-centre → left-centre
      const x1 = f.x + f.w, y1 = f.y + f.h / 2;
      const x2 = t.x,       y2 = t.y + t.h / 2;
      const gap = (x2 - x1);
      const cx1 = x1 + gap * 0.4, cx2 = x2 - gap * 0.4;
      paths.push(
        <path key={`f${edge.from}-${edge.to}`}
          d={`M${x1},${y1} C${cx1},${y1} ${cx2},${y2} ${x2},${y2}`}
          fill="none" stroke="hsl(217,20%,32%)" strokeWidth={1.5}
          markerEnd="url(#arr-fwd)"
        />,
      );
    } else {
      // Cycle arc: bottom-centre → bottom-centre, dipping below the graph
      const x1 = f.x + f.w / 2, y1 = f.y + f.h;
      const x2 = t.x + t.w / 2, y2 = t.y + t.h;
      const arcY = Math.max(y1, y2) + 52;
      const midX = (x1 + x2) / 2;
      paths.push(
        <g key={`c${edge.from}-${edge.to}`}>
          <path
            d={`M${x1},${y1} Q${midX},${arcY} ${x2},${y2}`}
            fill="none" stroke="hsl(350,65%,58%)" strokeWidth={1.5}
            strokeDasharray="5 3" markerEnd="url(#arr-cyc)" opacity={0.8}
          />
          {edge.label && (
            <text x={midX} y={arcY + 13} textAnchor="middle"
              fill="hsl(350,65%,65%)" fontSize={8.5}
              fontFamily="var(--font-sans)" opacity={0.9}>
              {edge.label}
            </text>
          )}
        </g>,
      );
    }
  }

  return (
    <svg
      style={{ position: "absolute", inset: 0, width: svgW, height: svgH,
        pointerEvents: "none", overflow: "visible" }}
    >
      <defs>
        <marker id="arr-fwd" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
          <path d="M0,0 L0,7 L7,3.5 z" fill="hsl(217,20%,32%)" />
        </marker>
        <marker id="arr-cyc" markerWidth="7" markerHeight="7" refX="5" refY="3.5" orient="auto">
          <path d="M0,0 L0,7 L7,3.5 z" fill="hsl(350,65%,58%)" />
        </marker>
      </defs>
      {paths}
    </svg>
  );
}

// ── Round card (forwardRef so parent can measure DOM position) ─────────────────

interface RoundCardProps {
  node: RoundNode;
  isCycleTarget: boolean;
  isSelected: boolean;
  onSelect: () => void;
}

const RoundCard = forwardRef<HTMLButtonElement, RoundCardProps>(
  function RoundCard({ node, isCycleTarget, isSelected, onSelect }, ref) {
    const pc = phaseColor(node.phase);
    const experts = node.actors.filter((a) => a.includes("."));
    const named   = node.actors.filter((a) => !a.includes("."));

    return (
      <button
        ref={ref}
        className={`pfg-card ${isSelected ? "pfg-card--sel" : ""}`}
        style={{ "--pfg-accent": pc } as React.CSSProperties}
        onClick={onSelect}
        title={node.rationale ?? `Round ${node.round}`}
      >
        {/* Top stripe shows phase colour */}
        <div className="pfg-card__stripe" style={{ background: pc }} />

        {/* Header row */}
        <div className="pfg-card__head">
          <span className="pfg-card__round-badge">
            {isCycleTarget && (
              <RotateCcw size={8} style={{ color: "hsl(350,70%,65%)", marginRight: 2 }} />
            )}
            R{node.round}
          </span>
          <span className="pfg-card__phase" style={{ color: pc }}>
            {prettyPhase(node.phase)}
          </span>
          <span className="pfg-card__status">
            {node.status === "completed" && <Check     size={11} style={{ color: "hsl(142,71%,48%)" }} />}
            {node.status === "running"   && <Activity  size={11} style={{ color: "hsl(217,91%,60%)", animation: "pulse 2s infinite" }} />}
            {node.status === "failed"    && <XCircle   size={11} style={{ color: "hsl(0,84%,60%)" }} />}
            {node.status === "pending"   && <Clock     size={11} style={{ color: "hsl(220,15%,50%)" }} />}
          </span>
        </div>

        {/* Agents */}
        <div className="pfg-card__agents">
          {named.map((a) => (
            <span key={a} className="pfg-card__chip"
              style={{ color: authorColor(a), borderColor: `${authorColor(a)}50`, background: `${authorColor(a)}14` }}>
              {prettyActor(a)}
            </span>
          ))}
          {experts.length === 1 && (
            <span className="pfg-card__chip"
              style={{ color: authorColor(experts[0]), borderColor: `${authorColor(experts[0])}50`, background: `${authorColor(experts[0])}14` }}>
              {prettyActor(experts[0])}
            </span>
          )}
          {experts.length > 1 && (
            <span className="pfg-card__group-chip">
              <Users size={9} />
              {experts.length} experts
            </span>
          )}
        </div>

        {/* Footer stats */}
        <div className="pfg-card__foot">
          <span className="pfg-card__stat">
            <Cpu size={8} />{node.turnCount}t
          </span>
          {node.durationMs != null && (
            <span className="pfg-card__stat">
              <Clock size={8} />{fmtMs(node.durationMs)}
            </span>
          )}
          {node.rationale && (
            <span className="pfg-card__info-hint">
              <Info size={8} /> why?
            </span>
          )}
        </div>
      </button>
    );
  },
);

// ── Detail side panel ─────────────────────────────────────────────────────────

function DetailPanel({ node, onClose }: { node: RoundNode; onClose: () => void }) {
  const pc = phaseColor(node.phase);
  return (
    <div className="pfg-detail">
      <div className="pfg-detail__header" style={{ borderLeftColor: pc }}>
        <div>
          <span className="pfg-detail__title">Round {node.round}</span>
          <span className="pfg-detail__phase" style={{ color: pc }}>
            {prettyPhase(node.phase)}
          </span>
        </div>
        <button className="pfg-detail__close" onClick={onClose} aria-label="Close">✕</button>
      </div>

      <div className="pfg-detail__body">
        {/* Agents section */}
        <div className="pfg-detail__section">
          <div className="pfg-detail__section-title">Agents activated</div>
          <div className="pfg-detail__agents">
            {node.actors.map((a) => (
              <div key={a} className="pfg-detail__agent">
                <span className="pfg-detail__agent-dot" style={{ background: authorColor(a) }} />
                <span className="pfg-detail__agent-name">{prettyActor(a)}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Stats */}
        <div className="pfg-detail__section">
          <div className="pfg-detail__section-title">Stats</div>
          <div className="pfg-detail__kv">
            <span>Turns</span><span>{node.turnCount}</span>
            {node.durationMs != null && <><span>Duration</span><span>{fmtMs(node.durationMs)}</span></>}
            <span>Status</span><span style={{ textTransform: "capitalize" }}>{node.status}</span>
          </div>
        </div>

        {/* Rationale */}
        {node.rationale && (
          <div className="pfg-detail__section">
            <div className="pfg-detail__section-title">
              <Info size={10} style={{ display: "inline", marginRight: 4 }} />
              Why this round?
            </div>
            <p className="pfg-detail__rationale">{node.rationale}</p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── Legend ─────────────────────────────────────────────────────────────────────

function GraphLegend({ hasCycles }: { hasCycles: boolean }) {
  return (
    <div className="pfg-legend">
      <span className="pfg-legend__item">
        <svg width="20" height="8" style={{ flexShrink: 0 }}>
          <line x1="0" y1="4" x2="14" y2="4" stroke="hsl(217,20%,32%)" strokeWidth="1.5" />
          <polygon points="11,1 17,4 11,7" fill="hsl(217,20%,32%)" />
        </svg>
        next round
      </span>
      {hasCycles && (
        <span className="pfg-legend__item">
          <svg width="20" height="8" style={{ flexShrink: 0 }}>
            <line x1="0" y1="4" x2="14" y2="4" stroke="hsl(350,65%,58%)" strokeWidth="1.5" strokeDasharray="4 2" />
            <polygon points="11,1 17,4 11,7" fill="hsl(350,65%,58%)" />
          </svg>
          debate cycle
        </span>
      )}
      <span className="pfg-legend__item">
        <Check size={9} style={{ color: "hsl(142,71%,48%)" }} /> done
      </span>
      <span className="pfg-legend__item">
        <Activity size={9} style={{ color: "hsl(217,91%,60%)" }} /> running
      </span>
      <span className="pfg-legend__hint">click any round for details</span>
    </div>
  );
}

// ── Root component ─────────────────────────────────────────────────────────────

interface ProcessFlowGraphProps {
  turns: TurnRecord[];
  narrations?: CoordinatorNarration[];
  isLive?: boolean;
}

export function ProcessFlowGraph({
  turns,
  narrations = [],
  isLive = false,
}: ProcessFlowGraphProps) {
  const layout = useMemo(() => buildFlowGraph(turns, narrations), [turns, narrations]);

  const [selectedIdx, setSelectedIdx] = useState<number | null>(null);

  const wrapRef  = useRef<HTMLDivElement>(null);
  const cardRefs = useRef<Map<number, HTMLButtonElement>>(new Map());

  const [pos,  setPos]  = useState<Map<number, NodePos>>(new Map());
  const [svgW, setSvgW] = useState(0);
  const [svgH, setSvgH] = useState(0);

  const measure = useCallback(() => {
    const wrap = wrapRef.current;
    if (!wrap) return;
    const wr = wrap.getBoundingClientRect();
    const map = new Map<number, NodePos>();
    let maxBottom = 0;
    cardRefs.current.forEach((el, idx) => {
      const r = el.getBoundingClientRect();
      const p = { x: r.left - wr.left, y: r.top - wr.top, w: r.width, h: r.height };
      map.set(idx, p);
      maxBottom = Math.max(maxBottom, p.y + p.h);
    });
    setPos(map);
    setSvgW(wr.width);
    setSvgH(maxBottom + 80); // extra room for cycle arcs below
  }, []);

  useEffect(() => {
    measure();
    const ro = new ResizeObserver(measure);
    if (wrapRef.current) ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, [measure, layout.nodes.length]);

  if (layout.nodes.length === 0) {
    return (
      <div className="pfg-empty">
        <Activity size={18} style={{ color: "var(--text-tertiary)" }} />
        <span>{isLive ? "Waiting for agent turns…" : "No execution data recorded."}</span>
      </div>
    );
  }

  const hasCycles  = layout.edges.some((e) => e.isCycle);
  const selected   = selectedIdx !== null ? layout.nodes[selectedIdx] : null;

  return (
    <div className="pfg-root">
      {/* Graph + detail panel */}
      <div className="pfg-body">
        {/* Scrollable canvas */}
        <div className="pfg-scroll">
          <div className="pfg-wrap" ref={wrapRef}
            style={{ minHeight: hasCycles ? svgH : undefined }}>
            {/* SVG edge layer */}
            <EdgeCanvas edges={layout.edges} pos={pos} svgW={svgW} svgH={svgH} />

            {/* Round nodes in a horizontal flex row */}
            <div className="pfg-row">
              {layout.nodes.map((node, idx) => (
                <RoundCard
                  key={node.round}
                  ref={(el) => {
                    if (el) cardRefs.current.set(idx, el);
                    else    cardRefs.current.delete(idx);
                  }}
                  node={node}
                  isCycleTarget={layout.cycleTargets.has(idx)}
                  isSelected={selectedIdx === idx}
                  onSelect={() => setSelectedIdx(selectedIdx === idx ? null : idx)}
                />
              ))}
            </div>
          </div>
        </div>

        {/* Detail panel */}
        {selected && (
          <DetailPanel node={selected} onClose={() => setSelectedIdx(null)} />
        )}
      </div>

      {/* Cycle annotation strip */}
      {hasCycles && (
        <div className="pfg-cycle-note">
          <GitMerge size={11} style={{ color: "hsl(350,65%,62%)", flexShrink: 0 }} />
          <span>
            Debate cycles detected — experts refined their findings after critique.
            Dashed arrows show the back-edge.
          </span>
        </div>
      )}

      {/* Legend */}
      <GraphLegend hasCycles={hasCycles} />
    </div>
  );
}

export default ProcessFlowGraph;
