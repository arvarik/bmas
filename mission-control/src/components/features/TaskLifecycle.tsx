"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  Ban,
  Check,
  Clock3,
  Copy,
  MessageSquarePlus,
  PauseCircle,
  Play,
  Send,
  Settings2,
  X,
} from "lucide-react";
import type { CostData, LogEntry, TaskMeta, TraceEvent } from "@/hooks/useTaskStream";
import {
  type TaskOperatorAction,
  type TaskOperatorResult,
  useTaskOperatorAction,
} from "@/hooks/useTaskOperatorAction";
import { ActionableError } from "@/components/ui/ActionableError";
import {
  operatorHistoryKey,
  parseOperatorHistory,
  serializeOperatorHistory,
  type StoredOperatorAction,
} from "@/lib/operator-action-history";
import { useFocusTrap } from "@/hooks/useFocusTrap";

interface OperationsStatus {
  providerReady: boolean;
  agentsOnline: number;
  agentsTotal: number;
  queued: number;
  queueCapacity: number;
}

type StageId = "queued" | "running" | "completed";
type BranchState = "blocked" | "failed" | "cancelled" | null;

const STAGES: { id: StageId; label: string }[] = [
  { id: "queued", label: "Queued" },
  { id: "running", label: "Running" },
  { id: "completed", label: "Completed" },
];

function currentStage(task: TaskMeta): StageId {
  if (task.status === "completed") return "completed";
  if (task.status === "running" || task.run_state === "recovering") return "running";
  return "queued";
}

function branchState(task: TaskMeta): BranchState {
  if (["aborted", "cancelled"].includes(task.run_state ?? "") || task.error_message?.startsWith("Task aborted")) return "cancelled";
  if (task.status === "failed") return "failed";
  if (["blocked", "paused", "pause_requested"].includes(task.run_state ?? "")) return "blocked";
  return null;
}

function stageState(stage: StageId, current: StageId): "done" | "current" | "future" {
  if (stage === current) return "current";
  if (stage === "queued") return "done";
  if (stage === "running" && current === "completed") return "done";
  return "future";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function actionSuccessMessage(action: TaskOperatorAction): string {
  if (action === "pause") return "Pause requested. The task will pause at the next safe boundary.";
  if (action === "resume") return "Resume requested. A blocked task will enter the queue when capacity is available.";
  if (action === "abort") return "Stop requested. The task will stop at the next safe boundary. Saved progress remains available.";
  return "Guidance queued. The runtime will apply it at the next supported boundary.";
}

function actionEventLabel(action: TaskOperatorAction): string {
  if (action === "pause") return "Operator requested a pause";
  if (action === "resume") return "Operator requested a resume";
  if (action === "abort") return "Operator requested a stop";
  return "Operator queued guidance";
}

export function TaskLifecycle({
  header,
  task,
  cost,
  isLive,
  isPaused,
  controls,
  logs,
  traceEvents,
}: {
  header: ReactNode;
  task: TaskMeta | null;
  cost: CostData | null;
  isLive: boolean;
  isPaused: boolean;
  controls: readonly string[];
  logs: LogEntry[];
  traceEvents: TraceEvent[];
}) {
  const router = useRouter();
  const taskId = task?.task_id ?? "";
  const [actionName, setActionName] = useState("");
  const [copyError, setCopyError] = useState("");
  const [guidance, setGuidance] = useState("");
  const [showGuidance, setShowGuidance] = useState(false);
  const [optimisticPaused, setOptimisticPaused] = useState<boolean | null>(null);
  const [operatorEvents, setOperatorEvents] = useState<StoredOperatorAction[]>([]);
  const [operations, setOperations] = useState<OperationsStatus | null>(null);
  const [operationsError, setOperationsError] = useState("");
  const [operationsVersion, setOperationsVersion] = useState(0);
  const [metadataOpen, setMetadataOpen] = useState(false);
  const metadataRef = useRef<HTMLDivElement>(null);
  const metadataButtonRef = useRef<HTMLButtonElement>(null);
  const {
    state: operatorAction,
    execute: executeOperatorAction,
    retry: retryOperatorAction,
  } = useTaskOperatorAction();

  useFocusTrap({
    active: metadataOpen,
    containerRef: metadataRef,
    returnFocusRef: metadataButtonRef,
    onEscape: () => setMetadataOpen(false),
  });

  useEffect(() => {
    if (!taskId) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- local history loads after hydration to keep server and client markup equal.
    setOperatorEvents(parseOperatorHistory(
      window.localStorage.getItem(operatorHistoryKey(taskId)),
    ));
  }, [taskId]);

  useEffect(() => {
    let cancelled = false;
    fetch("/api/readiness", { cache: "no-store" })
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        return body;
      })
      .then((body) => {
        if (cancelled) return;
        const agents = Object.values(body.agent_health ?? {}) as { alive?: boolean }[];
        setOperations({
          providerReady: body.litellm_connected === true,
          agentsOnline: agents.filter((agent) => agent.alive).length,
          agentsTotal: agents.length,
          queued: Number(body.task_queue?.queued_tasks ?? 0),
          queueCapacity: Number(body.task_queue?.queue_capacity ?? 0),
        });
        setOperationsError("");
      })
      .catch((error: unknown) => {
        if (!cancelled) {
          setOperationsError(error instanceof Error ? error.message : "Operations status is unavailable.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [operationsVersion]);

  const recordOperatorAction = useCallback((action: TaskOperatorAction) => {
    const timestamp = new Date().toISOString();
    setOperatorEvents((events) => {
      const next = [
        ...events,
        { id: `${action}:${timestamp}`, action, label: actionEventLabel(action), timestamp },
      ].slice(-20);
      if (taskId) {
        window.localStorage.setItem(
          operatorHistoryKey(taskId),
          serializeOperatorHistory(next),
        );
      }
      return next;
    });
  }, [taskId]);

  const clearOperatorHistory = useCallback(() => {
    setOperatorEvents([]);
    if (taskId) {
      window.localStorage.removeItem(operatorHistoryKey(taskId));
    }
  }, [taskId]);

  const applyOperatorResult = useCallback((
    action: TaskOperatorAction,
    result: TaskOperatorResult,
  ) => {
    if (!result.ok) return;
    if (action === "pause") setOptimisticPaused(true);
    if (action === "resume") setOptimisticPaused(false);
    if (action === "inject-hint") {
      setGuidance("");
      setShowGuidance(false);
    }
    recordOperatorAction(action);
    router.refresh();
  }, [recordOperatorAction, router]);

  const runControl = useCallback(async (action: "pause" | "resume" | "abort") => {
    if (!task || operatorAction.status === "pending") return;
    if (action === "abort" && !window.confirm(
      "Stop this task at the next safe boundary? Saved task history, Blackboard entries, files, and artifacts remain available.",
    )) return;
    const result = await executeOperatorAction(
      { action, task_id: task.task_id },
      actionSuccessMessage(action),
    );
    applyOperatorResult(action, result);
  }, [applyOperatorResult, executeOperatorAction, operatorAction.status, task]);

  const sendGuidance = useCallback(async () => {
    const text = guidance.trim();
    if (!task || !text || operatorAction.status === "pending") return;
    const action = "inject-hint" as const;
    const result = await executeOperatorAction(
      { action, task_id: task.task_id, hint_text: text },
      actionSuccessMessage(action),
    );
    applyOperatorResult(action, result);
  }, [applyOperatorResult, executeOperatorAction, guidance, operatorAction.status, task]);

  const retryLastOperatorAction = useCallback(async () => {
    const action = operatorAction.action;
    if (!action) return;
    const result = await retryOperatorAction(actionSuccessMessage(action));
    applyOperatorResult(action, result);
  }, [applyOperatorResult, operatorAction.action, retryOperatorAction]);

  const streamedPaused = isPaused || ["blocked", "paused", "pause_requested"].includes(task?.run_state ?? "");
  useEffect(() => {
    if (optimisticPaused !== null && streamedPaused === optimisticPaused) {
      // The task stream now confirms the requested state.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setOptimisticPaused(null);
    }
  }, [optimisticPaused, streamedPaused]);

  const copyTask = useCallback(async (purpose: "retry" | "duplicate") => {
    if (!task?.full_input) return;
    setActionName(purpose);
    setCopyError("");
    try {
      const response = await fetch(`/api/tasks/${encodeURIComponent(task.task_id)}/duplicate`, {
        method: "POST",
      });
      const body = await response.json().catch(() => ({})) as {
        task_id?: string;
        error?: string;
        detail?: string;
      };
      if (!response.ok || !body.task_id) {
        throw new Error(body.detail || body.error || `Task submission returned HTTP ${response.status}`);
      }
      router.push(`/task/${body.task_id}`);
    } catch (error) {
      setCopyError(error instanceof Error ? error.message : "The task copy failed.");
    } finally {
      setActionName("");
    }
  }, [router, task]);

  const stage = useMemo(() => task ? currentStage(task) : "queued", [task]);
  const timeline = useMemo(() => {
    if (!task) return [];
    const terminalBranch = branchState(task);
    return [
      { id: "created", timestamp: task.created_at, label: "System created the task", kind: "System" },
      ...logs.map((entry) => ({ id: `log:${entry.id}`, timestamp: entry.timestamp, label: entry.message, kind: entry.agent_role || "System" })),
      ...traceEvents.map((entry) => ({ id: `trace:${entry.id}`, timestamp: entry.timestamp, label: entry.content || entry.type, kind: entry.actor || "System" })),
      ...operatorEvents.map((entry) => ({ id: `operator:${entry.id}`, timestamp: entry.timestamp, label: entry.label, kind: "Operator" })),
      ...(task.completed_at ? [{ id: "terminal", timestamp: task.completed_at, label: terminalBranch === "cancelled" ? "Task cancelled" : task.status === "failed" ? "Task failed" : "Task completed", kind: "System" }] : []),
    ].filter((entry) => entry.timestamp).sort((left, right) => Date.parse(left.timestamp) - Date.parse(right.timestamp));
  }, [logs, operatorEvents, task, traceEvents]);
  if (!task) return <div className="task-command-header">{header}</div>;
  const branch = branchState(task);
  const storageBytes = (task.storage?.input_bytes ?? 0) + (task.storage?.output_bytes ?? 0);
  const blocked = branch === "blocked";
  const cancellable = isLive || task.status === "pending" || task.status === "running";
  const sourcePaused = streamedPaused;
  const displayPaused = optimisticPaused ?? sourcePaused;
  const canPause = !displayPaused && isLive && controls.includes("pause");
  const canResume = displayPaused && controls.includes("resume");
  const canAbort = cancellable && controls.includes("abort");
  const canSendGuidance = controls.includes("directive") && (isLive || blocked);
  const operatorPending = operatorAction.status === "pending";

  return (
    <section className="task-lifecycle" aria-label="Task lifecycle and operations">
      <div className="task-command-header">
        {header}
        <div className="task-lifecycle__timeline">
        {STAGES.map((item) => {
          const state = stageState(item.id, stage);
          return (
            <div key={item.id} className={`task-lifecycle__stage task-lifecycle__stage--${state}`}>
              <span>
                {state === "done" ? <Check size={12} /> : <Clock3 size={12} />}
              </span>
              <strong>{item.label}</strong>
            </div>
          );
        })}
        {branch ? <div className={`task-lifecycle__branch task-lifecycle__branch--${branch}`}><X size={12} /><strong>{branch.charAt(0).toUpperCase() + branch.slice(1)}</strong></div> : null}
      </div>

      <div className="task-lifecycle__actions" aria-busy={operatorPending}>
        {canPause ? (
          <button type="button" onClick={() => void runControl("pause")} disabled={operatorPending || !!actionName}>
            <PauseCircle size={14} /> {operatorAction.action === "pause" && operatorPending ? "Pausing…" : "Pause"}
          </button>
        ) : null}
        {canResume ? (
          <button type="button" onClick={() => void runControl("resume")} disabled={operatorPending || !!actionName}>
            <Play size={14} /> {operatorAction.action === "resume" && operatorPending ? "Resuming…" : "Resume"}
          </button>
        ) : null}
        {canAbort ? (
          <button type="button" onClick={() => void runControl("abort")} disabled={operatorPending || !!actionName}>
            <Ban size={14} /> {operatorAction.action === "abort" && operatorPending ? "Stopping…" : "Stop"}
          </button>
        ) : null}
        {canSendGuidance ? (
          <button
            type="button"
            onClick={() => setShowGuidance((visible) => !visible)}
            disabled={operatorPending || !!actionName}
            aria-expanded={showGuidance}
            aria-controls="task-operator-guidance"
          >
            <MessageSquarePlus size={14} /> Guide
          </button>
        ) : null}
        <button type="button" onClick={() => void copyTask("duplicate")} disabled={operatorPending || !!actionName || !task.full_input}>
          <Copy size={14} /> {actionName === "duplicate" ? "Duplicating…" : "Duplicate"}
        </button>
        {task.status === "failed" ? <button type="button" onClick={() => void copyTask("retry")} disabled={operatorPending || !!actionName || !task.full_input}><Play size={14} /> {actionName === "retry" ? "Retrying…" : "Retry"}</button> : null}
        <button ref={metadataButtonRef} type="button" onClick={() => setMetadataOpen(true)} aria-haspopup="dialog" aria-expanded={metadataOpen}><Settings2 size={14} /> Task data</button>
      </div>
      </div>

      {canSendGuidance && showGuidance ? (
        <div id="task-operator-guidance" className="overview__hint">
          <label className="overview__hint-label" htmlFor="task-operator-guidance-input">
            Operator guidance
          </label>
          <div className="overview__hint-row">
            <textarea
              id="task-operator-guidance-input"
              className="overview__hint-input"
              placeholder="Give the runtime guidance for the next supported boundary…"
              value={guidance}
              onChange={(event) => setGuidance(event.target.value)}
              rows={2}
              maxLength={2_000}
              disabled={operatorPending}
            />
            <button
              type="button"
              className="overview__hint-send"
              onClick={() => void sendGuidance()}
              disabled={!guidance.trim() || operatorPending}
              aria-label={operatorPending ? "Sending operator guidance" : "Send operator guidance"}
            >
              <Send size={14} />
            </button>
          </div>
        </div>
      ) : null}

      {task.status === "failed" && task.error_message ? (
        <div className="task-lifecycle__failure">
          <ActionableError
            component="Task execution"
            cause={task.error_message}
            timestamp={task.completed_at || task.last_heartbeat_at || task.created_at}
            onRetry={() => void copyTask("retry")}
            compact
          />
          <div><Link href={`/task/${task.task_id}/logs?log_q=${encodeURIComponent(task.error_message)}`}>Open related logs</Link><Link href={`/task/${task.task_id}/logs?mode=trace&trace_q=${encodeURIComponent(task.error_message)}`}>Open related traces</Link></div>
        </div>
      ) : null}
      {operatorAction.status === "error" ? (
        <ActionableError
          component="Task action"
          cause={operatorAction.message}
          onRetry={() => void retryLastOperatorAction()}
          compact
        />
      ) : null}
      {copyError ? (
        <ActionableError component="Task copy" cause={copyError} compact />
      ) : null}
      {operatorAction.status === "pending" || operatorAction.status === "success" ? (
        <div className="task-lifecycle__success" role="status" aria-live="polite">
          {operatorAction.message}
        </div>
      ) : null}
      {operatorEvents.length > 0 ? (
        <details className="task-lifecycle__history">
          <summary>Operator action history ({operatorEvents.length})</summary>
          <div
            role="log"
            aria-label="Operator actions saved in this browser"
            aria-live="polite"
          >
            <p>Saved in this browser for this task.</p>
            <ol>
              {[...operatorEvents].reverse().map((event) => (
                <li key={event.id}>
                  <strong>{event.label}</strong>{" "}
                  <time dateTime={event.timestamp}>{new Date(event.timestamp).toLocaleString()}</time>
                </li>
              ))}
            </ol>
            <button type="button" onClick={clearOperatorHistory}>Clear local history</button>
          </div>
        </details>
      ) : null}
      <details className="task-lifecycle__history">
        <summary>Operator and system event timeline ({timeline.length})</summary>
        <div role="log" aria-label="Chronological operator and system events">
          <ol>
            {timeline.map((entry) => <li key={entry.id}><strong>{entry.kind}</strong> {entry.label} <time dateTime={entry.timestamp}>{new Date(entry.timestamp).toLocaleString()}</time></li>)}
          </ol>
        </div>
      </details>
      {operationsError ? (
        <ActionableError
          component="Operations status"
          cause={operationsError}
          onRetry={() => setOperationsVersion((value) => value + 1)}
          compact
        />
      ) : null}
      {metadataOpen ? <>
        <button type="button" className="task-metadata-backdrop" aria-label="Close task data" onClick={() => setMetadataOpen(false)} />
        <div ref={metadataRef} className="task-metadata-drawer" role="dialog" aria-modal="true" aria-labelledby="task-metadata-title">
          <header><div><p>Task metadata</p><h3 id="task-metadata-title">Configuration and recovery</h3></div><button type="button" onClick={() => setMetadataOpen(false)} aria-label="Close task data"><X size={18} /></button></header>
          <p className="task-metadata-note">Mission Control captured these settings at submission. Session setting changes do not affect this active task.</p>
          <section><h4>Effective configuration</h4><pre>{JSON.stringify(task.effective_configuration ?? {}, null, 2)}</pre></section>
          <section><h4>Submission overrides</h4><pre>{JSON.stringify(task.submission_overrides ?? {}, null, 2)}</pre></section>
          <section><h4>Queue</h4><dl><div><dt>Queued tasks</dt><dd>{operations ? `${operations.queued}/${operations.queueCapacity}` : "Unavailable"}</dd></div><div><dt>Active agents</dt><dd>{operations ? `${operations.agentsOnline}/${operations.agentsTotal}` : "Unavailable"}</dd></div><div><dt>Provider</dt><dd>{operations?.providerReady ? "Healthy" : "Unavailable"}</dd></div></dl></section>
          <section><h4>Recovery</h4><dl><div><dt>Run state</dt><dd>{task.run_state || task.status}</dd></div><div><dt>Resume count</dt><dd>{task.resume_count ?? 0}</dd></div><div><dt>Event delivery</dt><dd>{task.event_delivery?.status || "Unknown"}</dd></div><div><dt>Latest cursor</dt><dd>{task.event_delivery?.latest_cursor ?? "None"}</dd></div></dl></section>
          <section><h4>Storage</h4><dl><div><dt>Inputs</dt><dd>{formatBytes(task.storage?.input_bytes ?? 0)}</dd></div><div><dt>Outputs</dt><dd>{formatBytes(task.storage?.output_bytes ?? 0)}</dd></div><div><dt>Total</dt><dd>{formatBytes(storageBytes)}</dd></div></dl></section>
          <section><h4>Usage</h4><dl><div><dt>Model</dt><dd>{task.model || "Selecting"}</dd></div><div><dt>Tokens</dt><dd>{(cost?.total_tokens ?? 0).toLocaleString()}</dd></div><div><dt>Cost</dt><dd>${(cost?.total_cost ?? 0).toFixed(4)}</dd></div></dl></section>
        </div>
      </> : null}
    </section>
  );
}
