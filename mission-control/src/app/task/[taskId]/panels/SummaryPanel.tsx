"use client";

/**
 * Task Overview Page — /task/[taskId]
 *
 * Three rendering modes:
 * - Running: live progress. The persistent task layout owns operator controls.
 * - Completed: result hero + process pipeline + stats + CTAs
 * - Failed: error card + retry button
 *
 */

import { useState, useEffect, Fragment, useMemo } from "react";
import type { ComponentType } from "react";
import Link from "next/link";
import { useParams, useSearchParams } from "next/navigation";

import { useTaskData } from "../TaskStreamContext";
import { Panel } from "@/components/ui/Panel";
import { MetricCard } from "@/components/ui/MetricCard";
import {
  Activity, Pause, XCircle,
  ChevronDown, Clock, Users,
  Layers, Zap, Radio, MessageSquare, Cpu, Cloud, FileText,
} from "lucide-react";
import { authorColor } from "@/lib/design-tokens";
import type { CostData, TaskArtifact, TurnRecord, CoordinatorNarration } from "@/hooks/useTaskStream";
import { ProcessFlowGraph } from "@/components/features/ProcessFlowGraph";
import { getActiveAdapter } from "@/lib/variants";
import { describeStopReason } from "@/lib/runtime-presentation";
import { UnsupportedVariantState } from "@/components/features/UnsupportedVariantState";


// ── Input Prompt Box (collapsible) ───────────────────────────────────

const PROMPT_COLLAPSE_LINES = 3;
const PROMPT_COLLAPSE_CHARS = 200;

function InputPromptBox({ prompt }: { prompt?: string }) {
  const [expanded, setExpanded] = useState(false);
  if (!prompt) return null;

  const isLong = prompt.length > PROMPT_COLLAPSE_CHARS || prompt.split("\n").length > PROMPT_COLLAPSE_LINES;

  return (
    <div
      className="overview__prompt-box"
      style={{
        padding: "var(--space-3) var(--space-4)",
        borderRadius: "var(--radius-md)",
        background: "var(--surface-overlay)",
        border: "1px solid var(--border-subtle)",
        marginBottom: "var(--space-4)",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: "var(--space-2)",
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div
            style={{
              fontSize: "10px",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              color: "var(--text-tertiary)",
              marginBottom: "var(--space-1)",
              fontWeight: "var(--weight-semibold)",
            }}
          >
            Input Prompt
          </div>
          <div
            style={{
              fontSize: "var(--text-sm)",
              color: "var(--text-secondary)",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              lineHeight: 1.5,
              ...(isLong && !expanded
                ? {
                    maxHeight: `${PROMPT_COLLAPSE_LINES * 1.5}em`,
                    overflow: "hidden",
                    maskImage: "linear-gradient(to bottom, black 60%, transparent 100%)",
                    WebkitMaskImage: "linear-gradient(to bottom, black 60%, transparent 100%)",
                  }
                : {}),
            }}
          >
            {prompt}
          </div>
        </div>
      </div>
      {isLong && (
        <button
          onClick={() => setExpanded(!expanded)}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            color: "var(--accent-primary)",
            fontSize: "var(--text-xs)",
            fontWeight: "var(--weight-medium)",
            padding: "var(--space-1) 0 0",
            display: "flex",
            alignItems: "center",
            gap: 4,
          }}
        >
          {expanded ? (
            <>
              <ChevronDown size={12} style={{ transform: "rotate(180deg)" }} />
              Show less
            </>
          ) : (
            <>
              <ChevronDown size={12} />
              Show more
            </>
          )}
        </button>
      )}
    </div>
  );
}

/** Map internal phase codes to human-readable names */
const PHASE_LABELS: Record<string, string> = {
  "control_plane:ag": "Agent Generator",
  "control_plane:cu": "Control Unit",
  "control_plane": "Control Plane",
  "trace": "Agent Execution",
  "triage": "Triage",
};

function prettyPhase(phase: string): string {
  return PHASE_LABELS[phase] ?? phase.replace(/_/g, " ");
}

// ── Model display helpers ─────────────────────────────────────────────

/** Human-readable model labels for internal aliases. */
const MODEL_LABELS: Record<string, { label: string; isLocal: boolean }> = {
  "edge-node-1": { label: "Gemma 4 E4B", isLocal: true },
  "edge-node-2": { label: "Gemma 4 E4B", isLocal: true },
  "edge-node-3": { label: "Gemma 4 E4B", isLocal: true },
  "gemini-pro":  { label: "Gemini Pro", isLocal: false },
  "gemini-flash": { label: "Gemini Flash", isLocal: false },
  "gemini-flash-lite": { label: "Gemini Flash Lite", isLocal: false },
};

function prettyModel(model: string | undefined): { label: string; isLocal: boolean } {
  if (!model) return { label: "Unknown", isLocal: false };
  const known = MODEL_LABELS[model];
  if (known) return known;
  // Fallback: if it starts with "edge-" assume local
  if (model.startsWith("edge-")) return { label: model, isLocal: true };
  // Otherwise show the raw alias, assume cloud
  return { label: model.replace(/-/g, " ").replace(/\b\w/g, c => c.toUpperCase()), isLocal: false };
}

/** Compact model badge showing local/cloud icon + name */
function ModelBadge({ model }: { model?: string }) {
  const info = prettyModel(model);
  const Icon = info.isLocal ? Cpu : Cloud;
  return (
    <span className="overview__model-badge" data-local={info.isLocal}>
      <Icon size={11} />
      <span>{info.isLocal ? "Local" : "Cloud"}</span>
      <span className="overview__model-badge-sep">·</span>
      <span className="overview__model-badge-name">{info.label}</span>
    </span>
  );
}

// ── Duration formatter ────────────────────────────────────────────────

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}m ${rem}s`;
}

// ── Cost display helper (interactive breakdowns) ──────────────────────

function CostDisplay({ cost }: { cost: CostData | null }) {
  const [expanded, setExpanded] = useState<"cost" | "tokens" | null>(null);
  if (!cost) {
    return (
      <div className="overview__stats">
        <MetricCard label="Total Cost" value="—" />
        <MetricCard label="Tokens" value="—" />
      </div>
    );
  }

  const modelEntries = Object.entries(cost.by_model);
  const phaseEntries = cost.by_phase ?? [];
  const actorEntries = cost.by_actor ?? [];

  return (
    <div>
      <div className="overview__stats">
        <button
          type="button"
          className={`overview__metric-toggle ${expanded === "cost" ? "overview__metric-toggle--active" : ""}`}
          onClick={() => setExpanded(expanded === "cost" ? null : "cost")}
          aria-expanded={expanded === "cost"}
          aria-controls="cost-breakdown"
        >
          <MetricCard
            label={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                Total Cost
                <ChevronDown
                  size={11}
                  style={{
                    transition: "transform 200ms ease",
                    transform: expanded === "cost" ? "rotate(180deg)" : "rotate(0deg)",
                    opacity: 0.5,
                  }}
                />
              </span>
            }
            value={cost.total_cost}
            format="currency"
          />
        </button>
        <button
          type="button"
          className={`overview__metric-toggle ${expanded === "tokens" ? "overview__metric-toggle--active" : ""}`}
          onClick={() => setExpanded(expanded === "tokens" ? null : "tokens")}
          aria-expanded={expanded === "tokens"}
          aria-controls="cost-breakdown"
        >
          <MetricCard
            label={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                Tokens
                <ChevronDown
                  size={11}
                  style={{
                    transition: "transform 200ms ease",
                    transform: expanded === "tokens" ? "rotate(180deg)" : "rotate(0deg)",
                    opacity: 0.5,
                  }}
                />
              </span>
            }
            value={cost.total_tokens}
            format="number"
          />
        </button>
      </div>

      {expanded && (
        <div
          id="cost-breakdown"
          className="overview__breakdown-panel"
          style={{
            padding: "var(--space-3)",
            borderRadius: "var(--radius-md)",
            background: "var(--surface-overlay)",
            border: "1px solid var(--border-subtle)",
            marginTop: "var(--space-2)",
            display: "flex",
            flexDirection: "column",
            gap: "var(--space-3)",
            fontSize: "var(--text-xs)",
            animation: "slide-down 200ms ease",
          }}
        >
          {/* By Model */}
          {modelEntries.length > 0 && (
            <CostBreakdownTable
              title="By Model"
              rows={modelEntries.map(([model, data]) => ({
                label: prettyModel(model).label + (prettyModel(model).isLocal ? " ⚡" : ""),
                cost: data.cost,
                tokens: data.tokens,
              }))}
              showField={expanded}
            />
          )}

          {/* By Actor */}
          {actorEntries.length > 0 && (
            <CostBreakdownTable
              title="By Actor"
              rows={actorEntries.map((a) => ({
                label: a.actor.replace(/_/g, " "),
                cost: a.cost_usd,
                tokens: a.tokens,
                extra: `${a.turns} turn${a.turns === 1 ? "" : "s"}`,
              }))}
              showField={expanded}
            />
          )}

          {/* By Phase */}
          {phaseEntries.length > 0 && (
            <CostBreakdownTable
              title="By Phase"
              rows={phaseEntries.map((p) => ({
                label: prettyPhase(p.phase ?? "unknown"),
                cost: p.cost_usd,
                tokens: p.tokens,
              }))}
              showField={expanded}
            />
          )}
        </div>
      )}
    </div>
  );
}

function CostBreakdownTable({
  title,
  rows,
  showField,
}: {
  title: string;
  rows: { label: string; cost: number; tokens: number; extra?: string }[];
  showField: "cost" | "tokens";
}) {
  const sorted = [...rows].sort((a, b) =>
    showField === "cost" ? b.cost - a.cost : b.tokens - a.tokens,
  );
  return (
    <div>
      <div
        style={{
          fontWeight: "var(--weight-semibold)",
          color: "var(--text-tertiary)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          fontSize: "10px",
          marginBottom: "var(--space-1)",
        }}
      >
        {title}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {sorted.map((row) => (
          <div
            key={row.label}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "var(--space-2)",
              padding: "3px 0",
              borderBottom: "1px solid var(--border-subtle)",
            }}
          >
            <span
              style={{
                flex: 1,
                color: "var(--text-secondary)",
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                textTransform: "capitalize",
              }}
            >
              {row.label}
            </span>
            {showField === "cost" ? (
              <span style={{ fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", color: "var(--text-primary)" }}>
                ${row.cost.toFixed(4)}
              </span>
            ) : (
              <span style={{ fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", color: "var(--text-primary)" }}>
                {row.tokens.toLocaleString()}
              </span>
            )}
            {row.extra && (
              <span style={{ color: "var(--text-tertiary)", fontStyle: "italic" }}>{row.extra}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Time display helper (duration breakdown + parallel timeline) ───────

function TimeDisplay({
  turns,
  totalMs,
}: {
  turns: TurnRecord[];
  totalMs?: number;
}) {
  const [expanded, setExpanded] = useState(false);

  if (!totalMs && turns.length === 0) return null;

  // Build timeline data from turns
  const timeline = turns
    .filter((t) => t.started_at)
    .map((t) => {
      const start = new Date(t.started_at).getTime();
      const end = t.ended_at ? new Date(t.ended_at).getTime() : start;
      return {
        actor: t.actor,
        role: (t.role ?? t.actor.split(".")[0]),
        start,
        end,
        duration: end - start,
        round: t.round_no,
      };
    })
    .filter((t) => !isNaN(t.start) && t.duration >= 0)
    .sort((a, b) => a.start - b.start);

  const globalStart = timeline.length > 0 ? Math.min(...timeline.map((t) => t.start)) : 0;
  const globalEnd = timeline.length > 0 ? Math.max(...timeline.map((t) => t.end)) : 0;
  const span = globalEnd - globalStart || 1;

  // Group by actor for swim lanes
  const actors = [...new Set(timeline.map((t) => t.actor))];

  // Compute parallel overlap
  let maxConcurrent = 1;
  if (timeline.length > 1) {
    const events: { time: number; delta: number }[] = [];
    for (const t of timeline) {
      events.push({ time: t.start, delta: 1 });
      events.push({ time: t.end, delta: -1 });
    }
    events.sort((a, b) => a.time - b.time || a.delta - b.delta);
    let concurrent = 0;
    for (const e of events) {
      concurrent += e.delta;
      maxConcurrent = Math.max(maxConcurrent, concurrent);
    }
  }

  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      <div className="overview__stats">
        <button
          type="button"
          className={`overview__metric-toggle ${expanded ? "overview__metric-toggle--active" : ""}`}
          onClick={() => setExpanded(!expanded)}
          aria-expanded={expanded}
          aria-controls="duration-breakdown"
        >
          <MetricCard
            label={
              <span style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
                <Clock size={11} style={{ opacity: 0.5 }} />
                Duration
                <ChevronDown
                  size={11}
                  style={{
                    transition: "transform 200ms ease",
                    transform: expanded ? "rotate(180deg)" : "rotate(0deg)",
                    opacity: 0.5,
                  }}
                />
              </span>
            }
            value={totalMs ? fmtDuration(totalMs) : "—"}
          />
        </button>
        <MetricCard
            label="Peak Parallelism"
            value={`${maxConcurrent} agent${maxConcurrent !== 1 ? "s" : ""}`}
          />
      </div>

      {expanded && timeline.length > 0 && (
        <div
          id="duration-breakdown"
          className="overview__breakdown-panel"
          style={{
            padding: "var(--space-3)",
            borderRadius: "var(--radius-md)",
            background: "var(--surface-overlay)",
            border: "1px solid var(--border-subtle)",
            marginTop: "var(--space-2)",
            animation: "slide-down 200ms ease",
          }}
        >
          {/* Gantt-style timeline */}
          <div
            style={{
              fontWeight: "var(--weight-semibold)",
              color: "var(--text-tertiary)",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              fontSize: "10px",
              marginBottom: "var(--space-2)",
            }}
          >
            Agent Timeline
          </div>
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "auto 1fr auto",
              gap: "6px var(--space-2)",
              alignItems: "center",
            }}
          >
            {actors.map((actor) => {
              const actorTurns = timeline.filter((t) => t.actor === actor);
              const color = authorColor(actor);
              const label = actor.split(".").pop() ?? actor;
              return (
                <Fragment key={actor}>
                  <span
                    style={{
                      fontSize: "10px",
                      color: "var(--text-tertiary)",
                      whiteSpace: "nowrap",
                      textTransform: "capitalize",
                    }}
                    title={actor}
                  >
                    {label}
                  </span>
                  <div
                    style={{
                      height: 14,
                      position: "relative",
                      background: "var(--surface-active)",
                      borderRadius: 3,
                      overflow: "hidden",
                    }}
                  >
                    {actorTurns.map((t, i) => {
                      const left = ((t.start - globalStart) / span) * 100;
                      const width = Math.max(((t.end - t.start) / span) * 100, 1);
                      return (
                        <div
                          key={i}
                          title={`${actor} R${t.round} — ${fmtDuration(t.duration)}`}
                          style={{
                            position: "absolute",
                            left: `${left}%`,
                            width: `${width}%`,
                            top: 1,
                            bottom: 1,
                            background: color,
                            borderRadius: 2,
                            opacity: 0.85,
                            transition: "opacity 150ms ease",
                          }}
                          onMouseEnter={(e) => { (e.target as HTMLElement).style.opacity = "1"; }}
                          onMouseLeave={(e) => { (e.target as HTMLElement).style.opacity = "0.85"; }}
                        />
                      );
                    })}
                  </div>
                  <span
                    style={{
                      fontSize: "10px",
                      fontFamily: "var(--font-mono)",
                      color: "var(--text-tertiary)",
                      textAlign: "right",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {fmtDuration(actorTurns.reduce((s, t) => s + t.duration, 0))}
                  </span>
                </Fragment>
              );
            })}

            {/* Time axis labels — spans under the Gantt bar column only */}
            <span />
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                marginTop: 2,
                fontSize: "9px",
                color: "var(--text-tertiary)",
                fontFamily: "var(--font-mono)",
              }}
            >
              <span>0s</span>
              <span>{fmtDuration(span)}</span>
            </div>
            <span />
          </div>
        </div>
      )}
    </div>
  );
}


// ── Elapsed Timer Hook ────────────────────────────────────────────────

function useElapsed(startIso: string | undefined, isLive: boolean): string {
  const [elapsed, setElapsed] = useState("");
  useEffect(() => {
    if (!isLive || !startIso) return;
    const start = new Date(startIso).getTime();
    const tick = () => setElapsed(fmtDuration(Math.max(0, Date.now() - start)));
    tick();
    const iv = setInterval(tick, 1000);
    return () => clearInterval(iv);
  }, [isLive, startIso]);
  return elapsed || "—";
}

// ── Output files strip ───────────────────────────────────────────────

/** Newest version of every produced file, linking to the Files tab. */
function OutputFilesStrip({ artifacts, live = false }: { artifacts: readonly TaskArtifact[]; live?: boolean }) {
  const { taskId } = useParams();
  const searchParams = useSearchParams();
  const newest = useMemo(() => {
    const byPath = new Map<string, TaskArtifact>();
    for (const artifact of artifacts) {
      const current = byPath.get(artifact.rel_path);
      if (!current || artifact.version > current.version) byPath.set(artifact.rel_path, artifact);
    }
    return [...byPath.values()].sort((left, right) => left.rel_path.localeCompare(right.rel_path));
  }, [artifacts]);
  if (newest.length === 0) return null;
  const params = new URLSearchParams(searchParams.toString());
  params.set("tab", "files");
  const filesHref = `/task/${taskId}?${params.toString()}`;
  return (
    <section className="output-files" aria-label="Output files">
      <div className="output-files__head">
        <h4 className="overview__section-label">Output files</h4>
        <Link href={filesHref}>Open Files</Link>
      </div>
      <div className="output-files__chips">
        {newest.map((artifact) => (
          <Link key={artifact.rel_path} href={filesHref} className="output-files__chip" title={`${artifact.rel_path} · version ${artifact.version}`}>
            <FileText size={13} aria-hidden="true" />
            <span className="output-files__name">{artifact.rel_path}</span>
            {artifact.version > 1 ? <span className="output-files__version">v{artifact.version}</span> : null}
          </Link>
        ))}
        {live ? <span className="output-files__live">updating live</span> : null}
      </div>
    </section>
  );
}

// ── Live Running View ─────────────────────────────────────────────────

interface LiveRunningViewProps {
  taskMeta: ReturnType<typeof useTaskData>["taskMeta"];
  artifacts: readonly TaskArtifact[];
  cost: CostData | null;
  completedTurns: TurnRecord[];
  activeTurns: TurnRecord[];
  boardEntries: ReturnType<typeof useTaskData>["boardEntries"];
  coordinatorNarrations: CoordinatorNarration[];
  consensus: ReturnType<typeof useTaskData>["consensus"];
  progressLabel: string;
}

function LiveRunningView({
  taskMeta,
  artifacts,
  cost,
  completedTurns,
  activeTurns,
  boardEntries,
  coordinatorNarrations,
  consensus,
  progressLabel,
}: LiveRunningViewProps) {
  const allTurns = useMemo(() => [...completedTurns, ...activeTurns], [completedTurns, activeTurns]);
  const elapsed = useElapsed(taskMeta?.created_at, true);

  // Derived live stats
  const activeActors = useMemo(() => {
    const set = new Set<string>();
    for (const t of activeTurns) set.add(t.actor);
    return set;
  }, [activeTurns]);

  const currentRound = useMemo(() => {
    let max = 0;
    for (const t of allTurns) max = Math.max(max, t.round_no);
    return max;
  }, [allTurns]);

  const totalTokens = cost?.total_tokens ?? 0;
  const totalCost = cost?.total_cost ?? 0;
  const latestNarration = coordinatorNarrations.length > 0
    ? coordinatorNarrations[coordinatorNarrations.length - 1]
    : null;

  return (
    <div className="view-container overview">
      <InputPromptBox prompt={taskMeta?.full_input} />

      {/* ── Live Dashboard ──────────────────────────────────────────── */}
      <div className="overview__live-dashboard">
        <div className="overview__live-header">
          <Radio size={14} style={{ color: "hsl(142, 71%, 45%)", animation: "pulse 2s infinite" }} />
          <span className="overview__live-label">Live</span>
          <span className="overview__live-phase">{progressLabel}</span>
        </div>

        <div className="overview__live-grid">
          <LiveStat icon={Clock} label="Elapsed" value={elapsed} accent />
          <LiveStat
            icon={Users}
            label="Active Agents"
            value={`${activeActors.size}`}
            detail={activeActors.size > 0 ? [...activeActors].map(a => a.split(".").pop()).join(", ") : undefined}
          />
          <LiveStat icon={Layers} label="Round" value={currentRound === 0 ? "Genesis" : `R${currentRound}`} />
          <LiveStat icon={MessageSquare} label="Board Entries" value={`${boardEntries.length}`} />
          <LiveStat
            icon={prettyModel(taskMeta?.model).isLocal ? Cpu : Cloud}
            label="Model"
            value={prettyModel(taskMeta?.model).label}
            detail={prettyModel(taskMeta?.model).isLocal ? "Local inference" : "Cloud API"}
          />
          <LiveStat
            icon={Zap}
            label="Tokens"
            value={totalTokens > 0 ? totalTokens.toLocaleString() : "—"}
          />
          <LiveStat
            icon={Activity}
            label="Cost"
            value={totalCost > 0 ? `$${totalCost.toFixed(4)}` : "—"}
          />
        </div>

        {/* Consensus indicator */}
        {consensus && consensus.signal > 0 && (
          <div className="overview__live-consensus">
            <span className="overview__live-consensus-label">Consensus</span>
            <div className="overview__live-consensus-bar">
              <div
                className="overview__live-consensus-fill"
                style={{ width: `${Math.min(consensus.signal * 100, 100)}%` }}
              />
            </div>
            <span className="overview__live-consensus-value">
              {Math.round(consensus.signal * 100)}%
            </span>
          </div>
        )}

        {/* Coordinator narration */}
        {latestNarration && latestNarration.rationale && (
          <div className="overview__live-narration">
            <span className="overview__live-narration-badge">R{latestNarration.round}</span>
            <span className="overview__live-narration-text">{latestNarration.rationale}</span>
          </div>
        )}
      </div>

      {/* ── Process Pipeline (live directed graph) ──────────────── */}
      <div className="overview__pipeline-section">
        <h4 className="overview__section-label">Execution Flow</h4>
        <ProcessFlowGraph
          turns={[...completedTurns, ...activeTurns]}
          narrations={coordinatorNarrations}
          isLive={true}
        />
      </div>

      {/* ── Active Agents ───────────────────────────────────────────── */}
      {activeActors.size > 0 && (
        <div className="overview__active-agents">
          <h4 className="overview__section-label">Active Agents</h4>
          <div className="overview__active-agents-grid">
            {[...activeActors].map((actor) => {
              const turn = activeTurns.find(t => t.actor === actor);
              const color = authorColor(actor);
              const displayName = actor.includes(".")
                ? actor.split(".")[1].replace(/_/g, " ")
                : actor.replace(/_/g, " ");
              return (
                <div key={actor} className="overview__active-agent-card" style={{ borderLeftColor: color }}>
                  <div className="overview__active-agent-header">
                    <span className="overview__active-agent-dot" style={{ background: color }} />
                    <span className="overview__active-agent-name">{displayName}</span>
                    {turn?.model && (
                      <span className="overview__active-agent-model">{turn.model}</span>
                    )}
                  </div>
                  <div className="overview__active-agent-meta">
                    {turn && <span>R{turn.round_no} · {turn.phase}</span>}
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      )}

      <OutputFilesStrip artifacts={artifacts} live />

      {/* Running cost breakdown */}
      <CostDisplay cost={cost} />
    </div>
  );
}

function LiveStat({
  icon: Icon,
  label,
  value,
  detail,
  accent,
}: {
  icon: React.ComponentType<{ size?: number; style?: React.CSSProperties }>;
  label: string;
  value: string;
  detail?: string;
  accent?: boolean;
}) {
  return (
    <div className="overview__live-stat">
      <div className="overview__live-stat-top">
        <Icon size={13} style={{ color: accent ? "var(--accent-primary)" : "var(--text-tertiary)", flexShrink: 0 }} />
        <span className="overview__live-stat-label">{label}</span>
      </div>
      <span
        className="overview__live-stat-value"
        style={accent ? { color: "var(--accent-primary)" } : undefined}
      >
        {value}
      </span>
      {detail && (
        <span className="overview__live-stat-detail">{detail}</span>
      )}
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────


export function SummaryPanel() {
  const streamData = useTaskData();
  const {
    result, error, isLive, taskMeta, cost,
    completedTurns, activeTurns, boardEntries, coordinatorNarrations, consensus,
    liveArtifacts, runtime,
  } = streamData;
  const adapter = getActiveAdapter(runtime.adapterId);
  const progressLabel = adapter
    ? adapter.progressLabel(streamData, runtime.capability?.features.progress ?? [])
    : "Initializing…";

  // Infer model from turns or cost data if it's missing from the initial task state
  const allTurns = useMemo(() => [...completedTurns, ...activeTurns], [completedTurns, activeTurns]);
  const inferredModel = useMemo(() => {
    if (taskMeta?.model && taskMeta.model !== "unknown") return taskMeta.model;
    const turnModel = allTurns.find((t) => t.model && t.model !== "unknown")?.model;
    if (turnModel) return turnModel;
    if (cost?.by_model) {
      const models = Object.keys(cost.by_model).filter((m) => m !== "unknown");
      if (models.length > 0) return models[0];
    }
    return undefined;
  }, [taskMeta, allTurns, cost]);

  const patchedTaskMeta = useMemo(() => {
    if (!taskMeta) return taskMeta;
    return { ...taskMeta, model: inferredModel ?? taskMeta.model };
  }, [taskMeta, inferredModel]);

  if (!adapter) return <UnsupportedVariantState runtime={runtime} />;

  // ── Running: live progress + HITL ─────────────────────────────────
  if (isLive) {
    return (
      <LiveRunningView
        taskMeta={patchedTaskMeta}
        artifacts={liveArtifacts}
        cost={cost}
        completedTurns={completedTurns}
        activeTurns={activeTurns}
        boardEntries={boardEntries}
        coordinatorNarrations={coordinatorNarrations}
        consensus={consensus}
        progressLabel={progressLabel}
      />
    );
  }

  // ── Completed: result hero + pipeline + stats ─────────────────────
  if (result && !error) {
    return <CompletedView
      result={result}
      taskMeta={patchedTaskMeta}
      artifacts={liveArtifacts}
      cost={cost}
      completedTurns={completedTurns}
      coordinatorNarrations={coordinatorNarrations}
      ResultRenderer={adapter.ResultRenderer}
      resultFormats={runtime.capability?.features.result ?? []}
    />;
  }

  // ── Failed: error card + retry ────────────────────────────────────
  if (error) {
    return (
      <div className="view-container overview">
        <InputPromptBox prompt={taskMeta?.full_input} />
        <Panel
          title="Task stopped"
          status="empty"
          emptyIcon={XCircle}
          emptyMessage="The task did not complete."
          emptyHint="Use Retry above to create a new task with the same inputs."
        />
      </div>
    );
  }

  if (["blocked", "paused", "pause_requested"].includes(taskMeta?.run_state ?? "")) {
    return (
      <div className="view-container overview">
        <InputPromptBox prompt={taskMeta?.full_input} />
        <Panel
          title="Task blocked"
          status="empty"
          emptyIcon={Pause}
          emptyMessage="This task needs operator attention."
          emptyHint="Correct the failed dependency, then use Resume above."
        />
      </div>
    );
  }

  // ── Pending: no data yet ──────────────────────────────────────────
  return (
    <div className="view-container overview">
      <Panel
        title="Task Overview"
        status="empty"
        emptyIcon={Activity}
        emptyMessage="No data yet"
        emptyHint="This task hasn't started running."
      />
    </div>
  );
}

// ── Completed View ─────────────────────────────────────────────────────

function CompletedView({
  result,
  taskMeta,
  artifacts,
  cost,
  completedTurns,
  coordinatorNarrations = [],
  ResultRenderer: AdapterResultRenderer,
  resultFormats,
}: {
  result: string;
  taskMeta: ReturnType<typeof useTaskData>["taskMeta"];
  artifacts: readonly TaskArtifact[];
  cost: CostData | null;
  completedTurns: TurnRecord[];
  coordinatorNarrations?: CoordinatorNarration[];
  ResultRenderer: ComponentType<{ content: string; formats: readonly string[] }>;
  resultFormats: readonly string[];
}) {
  const stopReason = taskMeta ? describeStopReason(taskMeta) : null;
  return (
    <div className="view-container overview">
      <InputPromptBox prompt={taskMeta?.full_input} />
      {/* Result hero */}
      <div className="overview__result-card">
        <div className="overview__result-head">
          <h3 className="overview__result-title">Result</h3>
          {stopReason ? (
            <span className={`task-header__stop task-header__stop--${stopReason.tone}`} title={stopReason.detail}>
              {stopReason.label}
            </span>
          ) : null}
        </div>
        <div className="overview__result-body">
          <AdapterResultRenderer content={result} formats={resultFormats} />
        </div>
      </div>

      <OutputFilesStrip artifacts={artifacts} />

      {/* Model badge */}
      {taskMeta?.model && (
        <div style={{ marginBottom: "var(--space-3)" }}>
          <ModelBadge model={taskMeta.model} />
        </div>
      )}

      {/* Process summary — directed execution graph */}
      <div className="overview__pipeline-section">
        <h4 className="overview__section-label">Execution Flow</h4>
        <ProcessFlowGraph
          turns={completedTurns}
          narrations={coordinatorNarrations}
          isLive={false}
        />
      </div>

      {/* Stats bar — interactive breakdown on click */}
      <CostDisplay cost={cost} />
      <TimeDisplay turns={completedTurns} totalMs={taskMeta?.duration_ms} />
    </div>
  );
}
