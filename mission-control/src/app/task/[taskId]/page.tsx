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
  Send, ArrowRight,
} from "lucide-react";
import type { StatusType } from "@/lib/design-tokens";

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
        {cost && (
          <div className="overview__running-stats">
            <MetricCard label="Running Cost" value={cost.total_cost} format="currency" />
            <MetricCard label="Tokens" value={cost.total_tokens} format="number" />
          </div>
        )}
      </div>
    );
  }

  // ── Completed: result hero + pipeline + stats ─────────────────────
  if (result && !error) {
    return (
      <div className="view-container overview">
        {/* Result hero */}
        <div className="overview__result-card">
          <h3 className="overview__result-title">Result</h3>
          <div className="overview__result-body">{result}</div>
        </div>

        {/* Process pipeline */}
        <div className="overview__pipeline-section">
          <h4 className="overview__section-label">Process Summary</h4>
          <div className="overview__pipeline">
            {PIPELINE_PHASES.map((p, i) => {
              const suffix = p.id;
              const st = subTasks.find((s) => s.id.endsWith(`-${suffix}`));
              const stStatus = st?.status ?? "pending";
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

        {/* Stats bar */}
        {cost && (
          <div className="overview__stats">
            <MetricCard label="Total Cost" value={cost.total_cost} format="currency" />
            <MetricCard label="Tokens" value={cost.total_tokens} format="number" />
            {taskMeta?.duration_ms && (
              <MetricCard label="Duration" value={fmtDuration(taskMeta.duration_ms)} />
            )}
          </div>
        )}

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
            View DAG →
          </Link>
        </div>
      </div>
    );
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
