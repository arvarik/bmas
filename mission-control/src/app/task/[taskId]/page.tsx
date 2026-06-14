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

import { useState, useCallback, useRef, useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useTaskData } from "./TaskStreamContext";
import { Panel } from "@/components/ui/Panel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { MetricCard } from "@/components/ui/MetricCard";
import {
  Activity, Check, Circle, AlertTriangle, Pause, Play, XCircle,
  Send, ArrowRight, ChevronDown, ChevronRight,
} from "lucide-react";
import type { StatusType } from "@/lib/design-tokens";
import type { CostData } from "@/hooks/useTaskStream";

// ── Status mapping ───────────────────────────────────────────────────

const STATUS_MAP: Record<string, StatusType> = {
  pending: "pending",
  running: "running",
  completed: "success",
  failed: "error",
};

// ── Phase labels for the process pipeline ─────────────────────────────

const PIPELINE_PHASES = [
  { id: "triage", label: "Triage" },
  { id: "plan",   label: "Planning" },
  { id: "exec",   label: "Execution" },
  { id: "audit",  label: "Audit" },
];

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
  const hasMarkdown =
    /^#{1,3}\s/m.test(trimmed) ||
    /^\s*[-*]\s/m.test(trimmed) ||
    /`[^`]+`/.test(trimmed) ||
    /^\d+\.\s/m.test(trimmed) ||
    /\*\*[^*]+\*\*/.test(trimmed);

  if (hasMarkdown) {
    return <MarkdownResultCard content={trimmed} />;
  }

  // Step 4: Plain text
  return <PlainResultCard content={trimmed} />;
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
                  {typeof value === "string" ? value : JSON.stringify(value)}
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

// ── Markdown-lite result card ─────────────────────────────────────────

function MarkdownResultCard({ content }: { content: string }) {
  const lines = content.split("\n");
  const rendered: React.ReactNode[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // Heading
    const headingMatch = line.match(/^(#{1,3})\s+(.+)/);
    if (headingMatch) {
      const level = headingMatch[1].length;
      rendered.push(
        <div key={i} className={`result-md-heading result-md-heading--h${level}`}>
          {headingMatch[2]}
        </div>
      );
      i++;
      continue;
    }

    // Numbered list item
    const numMatch = line.match(/^(\d+)\.\s+(.+)/);
    if (numMatch) {
      rendered.push(
        <div key={i} className="result-md-list-item">
          <span className="result-md-list-item__num">{numMatch[1]}.</span>
          <span className="result-md-list-item__text">{inlineFormat(numMatch[2])}</span>
        </div>
      );
      i++;
      continue;
    }

    // Bullet list item
    const bulletMatch = line.match(/^\s*[-*]\s+(.+)/);
    if (bulletMatch) {
      rendered.push(
        <div key={i} className="result-md-list-item">
          <span className="result-md-list-item__bullet">•</span>
          <span className="result-md-list-item__text">{inlineFormat(bulletMatch[1])}</span>
        </div>
      );
      i++;
      continue;
    }

    // Empty line → spacer
    if (line.trim() === "") {
      rendered.push(<div key={i} className="result-md-spacer" />);
      i++;
      continue;
    }

    // Regular paragraph
    rendered.push(
      <p key={i} className="result-md-para">{inlineFormat(line)}</p>
    );
    i++;
  }

  return <div className="result-markdown">{rendered}</div>;
}

/** Very lightweight inline formatter: bold, italic, code spans */
function inlineFormat(text: string): React.ReactNode {
  // Split by code spans first
  const parts = text.split(/(`[^`]+`)/);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith("`") && part.endsWith("`")) {
          return <code key={i} className="result-md-code">{part.slice(1, -1)}</code>;
        }
        // Bold **text**
        const boldParts = part.split(/(\*\*[^*]+\*\*)/);
        return (
          <span key={i}>
            {boldParts.map((bp, j) => {
              if (bp.startsWith("**") && bp.endsWith("**")) {
                return <strong key={j}>{bp.slice(2, -2)}</strong>;
              }
              return bp;
            })}
          </span>
        );
      })}
    </>
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

// ── Cost display helper ───────────────────────────────────────────────

function CostDisplay({ cost }: { cost: CostData | null }) {
  if (!cost) {
    return (
      <div className="overview__stats">
        <MetricCard label="Total Cost" value="—" />
        <MetricCard label="Tokens" value="—" />
      </div>
    );
  }
  return (
    <div className="overview__stats">
      <MetricCard label="Total Cost" value={cost.total_cost} format="currency" />
      <MetricCard label="Tokens" value={cost.total_tokens} format="number" />
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────

export default function TaskOverviewPage() {
  const { taskId } = useParams();
  const router = useRouter();
  const { phase, subTasks, result, error, isLive, taskMeta, cost } = useTaskData();

  const completedCount = subTasks.filter((st) => st.status === "completed").length;
  const totalCount = subTasks.length;

  // ── Running: live progress + HITL ─────────────────────────────────
  if (isLive) {
    return (
      <div className="view-container overview">
        <Panel
          title="Live Progress"
          subtitle={`Phase: ${phase ?? "Awaiting…"}`}
        >
          <div className="overview__progress">
            {/* Progress bar */}
            {totalCount > 0 && (
              <div className="overview__progress-section">
                <div className="overview__progress-label">
                  {completedCount} of {totalCount} sub-tasks completed
                </div>
                <div className="overview__progress-bar">
                  <div
                    className="overview__progress-fill"
                    style={{
                      width: `${totalCount > 0 ? (completedCount / totalCount) * 100 : 0}%`,
                    }}
                  />
                </div>
              </div>
            )}

            {/* Sub-task list */}
            {subTasks.map((st) => (
              <div key={st.id} className="overview__subtask">
                <StatusBadge status={STATUS_MAP[st.status] ?? "pending"} />
                <span className="overview__subtask-label">{st.label}</span>
                <span className="overview__subtask-agent">{st.agent}</span>
              </div>
            ))}

            {totalCount === 0 && (
              <div className="overview__awaiting">
                <Activity size={20} />
                <span>Awaiting swarm response…</span>
              </div>
            )}
          </div>
        </Panel>

        {/* HITL Controls */}
        <HITLControls taskId={taskId as string} />

        {/* Running cost */}
        <CostDisplay cost={cost} />
      </div>
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
  taskId,
}: {
  result: string;
  subTasks: ReturnType<typeof useTaskData>["subTasks"];
  taskMeta: ReturnType<typeof useTaskData>["taskMeta"];
  cost: CostData | null;
  taskId: string;
}) {
  // Determine pipeline phase statuses.
  // For a completed task, we show all phases that were executed.
  // We first try to find a matching sub-task, then fall back to
  // marking the phase as "completed" if the task itself is done.
  const isTaskCompleted = taskMeta?.status === "completed";

  function getPhaseStatus(phaseId: string): string {
    // Try matching sub-task by id suffix or label keyword
    const matchBySuffix = subTasks.find((s) => s.id.endsWith(`-${phaseId}`));
    if (matchBySuffix) return matchBySuffix.status;

    const matchByLabel = subTasks.find((s) =>
      s.label.toLowerCase().includes(phaseId) ||
      s.id.toLowerCase().includes(phaseId)
    );
    if (matchByLabel) return matchByLabel.status;

    // For completed tasks: if triage is done and no other sub-tasks exist,
    // infer that the subsequent phases ran to completion.
    if (isTaskCompleted) {
      const triageDone = subTasks.some(
        (s) => (s.id.includes("triage") || s.label.toLowerCase().includes("triage")) && s.status === "completed"
      );
      if (triageDone) return "completed";
    }

    return "pending";
  }

  const durationText = taskMeta?.duration_ms
    ? fmtDuration(taskMeta.duration_ms)
    : undefined;

  return (
    <div className="view-container overview">
      {/* Result hero */}
      <div className="overview__result-card">
        <h3 className="overview__result-title">Result</h3>
        <div className="overview__result-body">
          <ResultRenderer content={result} />
        </div>
      </div>

      {/* Process pipeline */}
      <div className="overview__pipeline-section">
        <h4 className="overview__section-label">Process Summary</h4>
        <div className="overview__pipeline">
          {PIPELINE_PHASES.map((p, i) => {
            const stStatus = getPhaseStatus(p.id);
            return (
              <div key={p.id} className="overview__pipeline-step-wrapper">
                <div className={`overview__pipeline-step overview__pipeline-step--${stStatus}`}>
                  {getPhaseIcon(stStatus)}
                  <span className="overview__pipeline-step-label">{p.label}</span>
                </div>
                {i < PIPELINE_PHASES.length - 1 && (
                  <ArrowRight size={14} className="overview__pipeline-arrow" />
                )}
              </div>
            );
          })}
        </div>
      </div>

      {/* Stats bar — always shown, dash when data unavailable */}
      <div className="overview__stats">
        {cost ? (
          <>
            <MetricCard label="Total Cost" value={cost.total_cost} format="currency" />
            <MetricCard label="Tokens" value={cost.total_tokens} format="number" />
          </>
        ) : (
          <>
            <MetricCard label="Total Cost" value="—" />
            <MetricCard label="Tokens" value="—" />
          </>
        )}
        {durationText && (
          <MetricCard label="Duration" value={durationText} />
        )}
      </div>

      {/* CTAs */}
      <div className="overview__ctas">
        <Link
          href={`/task/${taskId}/blackboard`}
          className="overview__cta"
        >
          View Full Debate →
        </Link>
        <Link
          href={`/task/${taskId}/dag`}
          className="overview__cta"
        >
          View Graph →
        </Link>
      </div>
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
