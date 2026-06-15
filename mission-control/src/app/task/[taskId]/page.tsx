"use client";

/**
 * Task Overview Page — /task/[taskId]
 *
 * Three rendering modes:
 * - Running: live progress + HITL controls (pause/abort/hint)
 * - Completed: result hero + process pipeline + stats + CTAs
 * - Failed: error card + retry button
 *
 */

import { useState, useCallback, useRef, useEffect, Fragment, useMemo } from "react";
import { useParams, useRouter } from "next/navigation";

import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useTaskData } from "./TaskStreamContext";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { MetricCard } from "@/components/ui/MetricCard";
import {
  Activity, Check, Circle, AlertTriangle, Pause, Play, XCircle,
  Send, ArrowRight, ChevronDown, ChevronRight, Clock, Users,
  Layers, Zap, Radio, MessageSquare,
} from "lucide-react";
import type { StatusType } from "@/lib/design-tokens";
import { authorColor } from "@/lib/design-tokens";
import type { CostData, TurnRecord, CoordinatorNarration } from "@/hooks/useTaskStream";

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

// ── Status mapping ───────────────────────────────────────────────────

const STATUS_MAP: Record<string, StatusType> = {
  pending: "pending",
  running: "running",
  completed: "success",
  failed: "error",
};

// ── Process summary stages (derived from real turn data) ──────────────

interface ProcessStage {
  key: string;
  label: string;
  status: "completed" | "running" | "failed" | "pending";
  detail?: string;
}

// Canonical traditional-blackboard role ordering for the process summary.
const ROLE_STAGE_ORDER = [
  "planner", "expert", "critic", "conflict_resolver", "cleaner", "decider",
];

const ROLE_STAGE_LABELS: Record<string, string> = {
  planner: "Planning",
  expert: "Expert Analysis",
  critic: "Critique",
  conflict_resolver: "Conflict Resolution",
  cleaner: "Board Cleanup",
  decider: "Decision",
};

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

function stageLabelForRole(role: string): string {
  const base = role.split(".")[0];
  if (ROLE_STAGE_LABELS[base]) return ROLE_STAGE_LABELS[base];
  return base.charAt(0).toUpperCase() + base.slice(1).replace(/_/g, " ");
}

/**
 * Build the process summary from the *actual* stages that occurred.
 *
 * The daemon writes four static sub-tasks (triage/plan/exec/audit) but the
 * traditional blackboard loop only ever marks triage complete — so keying
 * the summary off sub-tasks made every task look like "triage only". The
 * real signal is the per-turn record (planner → experts → critic → decider,
 * across rounds), which we group into named stages here.
 */
function buildProcessStages(
  subTasks: ReturnType<typeof useTaskData>["subTasks"],
  allTurns: TurnRecord[],
  taskMeta: ReturnType<typeof useTaskData>["taskMeta"],
): ProcessStage[] {
  const stages: ProcessStage[] = [];
  const taskDone = taskMeta?.status === "completed";
  const taskFailed = taskMeta?.status === "failed";
  const isRunning = taskMeta?.status === "running";

  // 1. Triage — completed once any turn exists (meaning the orchestrator
  //    has passed triage and entered the round loop).
  const triage = subTasks.find(
    (s) => s.id.includes("triage") || s.label.toLowerCase().includes("triage"),
  );
  const triageStatus: ProcessStage["status"] =
    (triage?.status === "completed" || allTurns.length > 0)
      ? "completed"
      : isRunning
        ? "running"
        : taskDone ? "completed" : "pending";
  stages.push({
    key: "triage",
    label: "Triage",
    status: triageStatus,
    detail: taskMeta?.complexity
      ? `${taskMeta.complexity} complexity — routes to appropriate model tier`
      : undefined,
  });

  // 2. Stages from real turns, grouped by base role
  const groups = new Map<string, TurnRecord[]>();
  for (const t of allTurns) {
    const base = (t.actor || "agent").split(".")[0];
    const arr = groups.get(base) ?? [];
    arr.push(t);
    groups.set(base, arr);
  }
  const orderedRoles = [
    ...ROLE_STAGE_ORDER.filter((r) => groups.has(r)),
    ...[...groups.keys()].filter((r) => !ROLE_STAGE_ORDER.includes(r)),
  ];
  for (const role of orderedRoles) {
    const turns = groups.get(role)!;
    const anyFailed = turns.some((t) => t.status === "failed");
    const anyActive = turns.some((t) => t.status === "active" || t.status === "running");
    const rounds = [...new Set(turns.map((t) => t.round_no).filter((n) => n > 0))];
    const parts: string[] = [`${turns.length} turn${turns.length === 1 ? "" : "s"}`];
    if (rounds.length === 1) parts.push(`round ${rounds[0]}`);
    else if (rounds.length > 1) parts.push(`rounds ${Math.min(...rounds)}\u2013${Math.max(...rounds)}`);
    stages.push({
      key: `role-${role}`,
      label: stageLabelForRole(role),
      status: anyFailed ? "failed" : anyActive ? "running" : "completed",
      detail: parts.join(" \u00b7 "),
    });
  }

  // 3. Completion
  stages.push({
    key: "complete",
    label: taskFailed ? "Failed" : "Completed",
    status: taskFailed ? "failed" : taskDone ? "completed" : "pending",
  });

  return stages;
}

function getPhaseIcon(status: string) {
  switch (status) {
    case "completed": return <Check size={14} />;
    case "running":   return <Activity size={14} />;
    case "failed":    return <XCircle size={14} />;
    default:          return <Circle size={14} />;
  }
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

// ── Smart Result Renderer ─────────────────────────────────────────────
// The BMAS daemon stores result_summary as a markdown-fenced JSON blob
// containing a board entry: ```json\n{ "body": "...", "title": "...", ... }\n```
// We extract the `body` (the actual human answer) and render it properly.
// Falls back to rendering the raw content as markdown or plain text.

/** Extract JSON from a ```json ... ``` markdown code fence */
function extractFencedJson(text: string): unknown | null {
  const fenceMatch = text.match(/```(?:json)?\s*\n([\s\S]+?)\n```/);
  if (fenceMatch) {
    try { return JSON.parse(fenceMatch[1]); } catch { /* not valid JSON */ }
  }
  return null;
}

/** Pull the human-readable answer from a BMAS board entry object */
function extractBoardBody(obj: unknown): string | null {
  if (typeof obj !== "object" || obj === null) return null;
  const entry = obj as Record<string, unknown>;
  // board entry: { body: "...", title: "...", type: "solution", ... }
  if (typeof entry.body === "string" && entry.body.trim()) {
    return entry.body.trim();
  }
  // array of board entries — pick the solution/finding with highest confidence
  return null;
}

/** Same extraction for arrays */
function extractFromArray(arr: unknown[]): string | null {
  // Look for the solution-type entry first
  const solution = arr.find(
    (e) => typeof e === "object" && e !== null && (e as Record<string,unknown>).type === "solution"
  ) ?? arr[arr.length - 1];
  return extractBoardBody(solution);
}

function ResultRenderer({ content }: { content: string }) {
  const trimmed = content.trim();

  // Step 1: Try to extract from markdown-fenced JSON (BMAS primary format)
  const fencedObj = extractFencedJson(trimmed);
  if (fencedObj !== null) {
    // Array of board entries
    if (Array.isArray(fencedObj)) {
      const body = extractFromArray(fencedObj);
      if (body) return <MarkdownResultCard content={body} />;
    }
    // Single board entry
    const body = extractBoardBody(fencedObj);
    if (body) return <MarkdownResultCard content={body} />;
    // Fallback: render the JSON structure
    return <JsonResultCard data={fencedObj} />;
  }

  // Step 2: Bare JSON (no fence)
  let parsed: unknown = null;
  try { parsed = JSON.parse(trimmed); } catch { /* not JSON */ }
  if (parsed !== null) {
    if (Array.isArray(parsed)) {
      const body = extractFromArray(parsed);
      if (body) return <MarkdownResultCard content={body} />;
    }
    const body = extractBoardBody(parsed);
    if (body) return <MarkdownResultCard content={body} />;
    return <JsonResultCard data={parsed} />;
  }

  // Step 3: Markdown-like plain text
  if (looksLikeMarkdown(trimmed)) {
    return <MarkdownResultCard content={trimmed} />;
  }

  // Step 4: Plain text
  return <PlainResultCard content={trimmed} />;
}

/** Heuristic: does this string contain markdown syntax worth rendering? */
function looksLikeMarkdown(text: string): boolean {
  return (
    /^#{1,6}\s/m.test(text) ||      // headings (any level)
    /^\s*[-*+]\s/m.test(text) ||    // bullet lists
    /`[^`]+`/.test(text) ||         // inline code
    /^\d+\.\s/m.test(text) ||       // numbered lists
    /\*\*[^*]+\*\*/.test(text) ||   // bold
    /\[[^\]]+\]\([^)]+\)/.test(text) || // links
    /^\s*\|.+\|/m.test(text)        // tables
  );
}

// ── JSON result card ─────────────────────────────────────────────────

function JsonResultCard({ data }: { data: unknown }) {
  if (typeof data === "string") {
    return <PlainResultCard content={data} />;
  }
  if (Array.isArray(data)) {
    return (
      <div className="result-json-array">
        {data.map((item, i) => (
          <div key={i} className="result-json-array__item">
            <span className="result-json-array__index">{i + 1}</span>
            <span className="result-json-value">
              {typeof item === "object" && item !== null
                ? <JsonObjectCard data={item as Record<string, unknown>} depth={1} />
                : String(item)}
            </span>
          </div>
        ))}
      </div>
    );
  }
  if (typeof data === "object" && data !== null) {
    return <JsonObjectCard data={data as Record<string, unknown>} depth={0} />;
  }
  return <PlainResultCard content={String(data)} />;
}

function JsonObjectCard({ data, depth }: { data: Record<string, unknown>; depth: number }) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const entries = Object.entries(data);

  const toggleKey = (k: string) => setExpanded(prev => ({ ...prev, [k]: !prev[k] }));

  return (
    <div className={`result-json-object ${depth === 0 ? "result-json-object--root" : ""}`}>
      {entries.map(([key, value]) => {
        const isNested = typeof value === "object" && value !== null && !Array.isArray(value);
        const isArray = Array.isArray(value);
        const isComplex = isNested || isArray;
        const isOpen = expanded[key] !== false; // default open at depth 0

        return (
          <div key={key} className="result-json-row">
            <div
              className={`result-json-row__header ${isComplex ? "result-json-row__header--clickable" : ""}`}
              onClick={isComplex ? () => toggleKey(key) : undefined}
            >
              {isComplex && (
                <span className="result-json-row__chevron">
                  {isOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                </span>
              )}
              <span className="result-json-row__key">{key}</span>
              {!isComplex && (
                <span className="result-json-row__value">
                  {typeof value === "string"
                    ? (looksLikeMarkdown(value)
                        ? <MarkdownResultCard content={value} />
                        : value)
                    : JSON.stringify(value)}
                </span>
              )}
              {isComplex && !isOpen && (
                <span className="result-json-row__collapsed-hint">
                  {isArray ? `[${(value as unknown[]).length} items]` : "{…}"}
                </span>
              )}
            </div>
            {isComplex && isOpen && (
              <div className="result-json-row__children">
                {isArray
                  ? <JsonResultCard data={value} />
                  : <JsonObjectCard data={value as Record<string, unknown>} depth={depth + 1} />
                }
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Markdown result card ──────────────────────────────────────────────
// Full CommonMark + GitHub-flavored markdown (headings of any level,
// tables, task lists, strikethrough, fenced code, etc.). The previous
// hand-rolled renderer only understood h1–h3, so deeper headings (####)
// and tables leaked through as raw text.

function MarkdownResultCard({ content }: { content: string }) {
  return (
    <div className="result-markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{content}</ReactMarkdown>
    </div>
  );
}

// ── Plain text result card ────────────────────────────────────────────

function PlainResultCard({ content }: { content: string }) {
  // Split paragraphs on double newlines
  const paragraphs = content.split(/\n\n+/);
  if (paragraphs.length === 1) {
    return <p className="result-plain">{content}</p>;
  }
  return (
    <div className="result-plain-multi">
      {paragraphs.map((para, i) => (
        <p key={i} className="result-plain-para">{para}</p>
      ))}
    </div>
  );
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
        <div
          className={`overview__metric-toggle ${expanded === "cost" ? "overview__metric-toggle--active" : ""}`}
          onClick={() => setExpanded(expanded === "cost" ? null : "cost")}
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
        </div>
        <div
          className={`overview__metric-toggle ${expanded === "tokens" ? "overview__metric-toggle--active" : ""}`}
          onClick={() => setExpanded(expanded === "tokens" ? null : "tokens")}
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
        </div>
      </div>

      {expanded && (
        <div
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
                label: model,
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
        <div
          className={`overview__metric-toggle ${expanded ? "overview__metric-toggle--active" : ""}`}
          onClick={() => setExpanded(!expanded)}
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
        </div>
        <MetricCard
            label="Peak Parallelism"
            value={`${maxConcurrent} agent${maxConcurrent !== 1 ? "s" : ""}`}
          />
      </div>

      {expanded && timeline.length > 0 && (
        <div
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

// ── Live Running View ─────────────────────────────────────────────────

interface LiveRunningViewProps {
  phase: string | null;
  subTasks: ReturnType<typeof useTaskData>["subTasks"];
  taskMeta: ReturnType<typeof useTaskData>["taskMeta"];
  cost: CostData | null;
  taskId: string;
  completedTurns: TurnRecord[];
  activeTurns: TurnRecord[];
  boardEntries: ReturnType<typeof useTaskData>["boardEntries"];
  coordinatorNarrations: CoordinatorNarration[];
  consensus: ReturnType<typeof useTaskData>["consensus"];
}

function LiveRunningView({
  phase,
  subTasks,
  taskMeta,
  cost,
  taskId,
  completedTurns,
  activeTurns,
  boardEntries,
  coordinatorNarrations,
  consensus,
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
          <span className="overview__live-phase">{phase ?? "Initializing…"}</span>
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

      {/* ── Process Pipeline (live) ─────────────────────────────────── */}
      <div className="overview__pipeline-section">
        <h4 className="overview__section-label">Process Pipeline</h4>
        <div className="overview__stages">
          {buildProcessStages(subTasks, allTurns, taskMeta).map((s, i, arr) => (
            <div key={s.key} className="overview__stage-row">
              <div className={`overview__stage overview__stage--${s.status}`}>
                <div className="overview__stage-head">
                  <span className="overview__stage-icon">{getPhaseIcon(s.status)}</span>
                  <span className="overview__stage-label">{s.label}</span>
                </div>
                {s.detail && (
                  <span className="overview__stage-detail">{s.detail}</span>
                )}
              </div>
              {i < arr.length - 1 && (
                <ArrowRight size={13} className="overview__stage-arrow" />
              )}
            </div>
          ))}
        </div>
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

      {/* HITL Controls */}
      <HITLControls taskId={taskId} />

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


export default function TaskOverviewPage() {
  const { taskId } = useParams();
  const router = useRouter();
  const {
    phase, subTasks, result, error, isLive, taskMeta, cost,
    completedTurns, activeTurns, boardEntries, coordinatorNarrations, consensus,
  } = useTaskData();

  // ── Running: live progress + HITL ─────────────────────────────────
  if (isLive) {
    return (
      <LiveRunningView
        phase={phase}
        subTasks={subTasks}
        taskMeta={taskMeta}
        cost={cost}
        taskId={taskId as string}
        completedTurns={completedTurns}
        activeTurns={activeTurns}
        boardEntries={boardEntries}
        coordinatorNarrations={coordinatorNarrations}
        consensus={consensus}
      />
    );
  }

  // ── Completed: result hero + pipeline + stats ─────────────────────
  if (result && !error) {
    return <CompletedView
      result={result}
      subTasks={subTasks}
      taskMeta={taskMeta}
      cost={cost}
      taskId={taskId as string}
      completedTurns={completedTurns}
    />;
  }

  // ── Failed: error card + retry ────────────────────────────────────
  if (error) {
    return (
      <div className="view-container overview">
        <div className="overview__error-card">
          <div className="overview__error-header">
            <AlertTriangle size={20} />
            <h3>Task Failed</h3>
          </div>
          <div className="overview__error-body">{error}</div>
          <button
            className="overview__retry-btn"
            onClick={async () => {
              // Re-submit the same input as a new task
              try {
                const res = await fetch("/api/submit", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({
                    task: taskMeta?.label ?? "",
                  }),
                });
                if (res.ok) {
                  const data = await res.json();
                  if (data.task_id) {
                    router.push(`/task/${data.task_id}`);
                  }
                }
              } catch {
                // Retry is best-effort
              }
            }}
          >
            Retry Task →
          </button>
        </div>
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
  subTasks,
  taskMeta,
  cost,
  taskId: _taskId,
  completedTurns,
}: {
  result: string;
  subTasks: ReturnType<typeof useTaskData>["subTasks"];
  taskMeta: ReturnType<typeof useTaskData>["taskMeta"];
  cost: CostData | null;
  taskId: string;
  completedTurns: TurnRecord[];
}) {
  const stages = buildProcessStages(subTasks, completedTurns, taskMeta);

  const _durationText = taskMeta?.duration_ms
    ? fmtDuration(taskMeta.duration_ms)
    : undefined;

  return (
    <div className="view-container overview">
      <InputPromptBox prompt={taskMeta?.full_input} />
      {/* Result hero */}
      <div className="overview__result-card">
        <h3 className="overview__result-title">Result</h3>
        <div className="overview__result-body">
          <ResultRenderer content={result} />
        </div>
      </div>

      {/* Process summary — derived from the actual stages/turns that ran */}
      <div className="overview__pipeline-section">
        <h4 className="overview__section-label">Process Summary</h4>
        <div className="overview__stages">
          {stages.map((s, i) => (
            <div key={s.key} className="overview__stage-row">
              <div className={`overview__stage overview__stage--${s.status}`}>
                <div className="overview__stage-head">
                  <span className="overview__stage-icon">{getPhaseIcon(s.status)}</span>
                  <span className="overview__stage-label">{s.label}</span>
                </div>
                {s.detail && (
                  <span className="overview__stage-detail">{s.detail}</span>
                )}
              </div>
              {i < stages.length - 1 && (
                <ArrowRight size={13} className="overview__stage-arrow" />
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Stats bar — interactive breakdown on click */}
      <CostDisplay cost={cost} />
      <TimeDisplay turns={completedTurns} totalMs={taskMeta?.duration_ms} />
    </div>
  );
}

// ── HITL Controls ─────────────────────────────────────────────────────

function HITLControls({ taskId }: { taskId: string }) {
  const [isPaused, setIsPaused] = useState(false);
  const [isAborting, setIsAborting] = useState(false);
  const [hintText, setHintText] = useState("");
  const [hintSending, setHintSending] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // Check current pause state on mount
  useEffect(() => {
    fetch("/api/hitl")
      .then((r) => r.json())
      .then((d) => setIsPaused(d.paused ?? false))
      .catch(() => {});
  }, []);

  const handlePauseToggle = useCallback(async () => {
    const action = isPaused ? "resume" : "pause";
    try {
      const res = await fetch("/api/hitl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action }),
      });
      if (res.ok) setIsPaused(!isPaused);
    } catch {}
  }, [isPaused]);

  const handleAbort = useCallback(async () => {
    if (!confirm("Stop this task? Any progress will be lost.")) return;
    setIsAborting(true);
    try {
      await fetch("/api/hitl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "abort", task_id: taskId }),
      });
    } catch {}
  }, [taskId]);

  const handleSendHint = useCallback(async () => {
    if (!hintText.trim()) return;
    setHintSending(true);
    try {
      await fetch("/api/hitl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "inject-hint",
          task_id: taskId,
          hint_text: hintText.trim(),
        }),
      });
      setHintText("");
    } catch {}
    finally { setHintSending(false); }
  }, [taskId, hintText]);

  return (
    <div className="overview__hitl">
      <h4 className="overview__section-label">Operator Controls</h4>
      <div className="overview__hitl-buttons">
        <button
          className={`overview__hitl-btn ${isPaused ? "overview__hitl-btn--resume" : "overview__hitl-btn--pause"}`}
          onClick={handlePauseToggle}
        >
          {isPaused ? <Play size={14} /> : <Pause size={14} />}
          {isPaused ? "Resume Swarm" : "Pause Swarm"}
        </button>
        <button
          className="overview__hitl-btn overview__hitl-btn--abort"
          onClick={handleAbort}
          disabled={isAborting}
        >
          <XCircle size={14} />
          {isAborting ? "Aborting…" : "Abort Task"}
        </button>
      </div>

      {/* Hint injection */}
      <div className="overview__hint">
        <label className="overview__hint-label" htmlFor="hint-input">
          Inject Hint
        </label>
        <div className="overview__hint-row">
          <textarea
            id="hint-input"
            ref={textareaRef}
            className="overview__hint-input"
            placeholder="Enter guidance for the swarm…"
            value={hintText}
            onChange={(e) => setHintText(e.target.value)}
            rows={2}
            disabled={hintSending}
          />
          <button
            className="overview__hint-send"
            onClick={handleSendHint}
            disabled={!hintText.trim() || hintSending}
          >
            <Send size={14} />
          </button>
        </div>
      </div>
    </div>
  );
}
