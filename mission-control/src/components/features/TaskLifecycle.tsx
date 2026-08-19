"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import {
  Ban,
  Check,
  Clock3,
  Copy,
  PauseCircle,
  Play,
  X,
} from "lucide-react";
import type { CostData, TaskMeta } from "@/hooks/useTaskStream";
import { ActionableError } from "@/components/ui/ActionableError";

interface OperationsStatus {
  providerReady: boolean;
  agentsOnline: number;
  agentsTotal: number;
  queued: number;
  queueCapacity: number;
}

type StageId = "queued" | "running" | "blocked" | "failed" | "completed";

const STAGES: { id: StageId; label: string }[] = [
  { id: "queued", label: "Queued" },
  { id: "running", label: "Running" },
  { id: "blocked", label: "Blocked" },
  { id: "failed", label: "Failed" },
  { id: "completed", label: "Completed" },
];

function currentStage(task: TaskMeta): StageId {
  if (task.status === "failed") return "failed";
  if (task.status === "completed") return "completed";
  if (["blocked", "paused", "pause_requested"].includes(task.run_state ?? "")) {
    return "blocked";
  }
  if (task.status === "running" || task.run_state === "recovering") return "running";
  return "queued";
}

function stageState(stage: StageId, current: StageId): "done" | "current" | "future" {
  if (stage === current) return "current";
  if (stage === "queued") return "done";
  if (stage === "running" && ["blocked", "failed", "completed"].includes(current)) return "done";
  return "future";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function TaskLifecycle({
  task,
  cost,
  isLive,
}: {
  task: TaskMeta | null;
  cost: CostData | null;
  isLive: boolean;
}) {
  const router = useRouter();
  const [actionError, setActionError] = useState("");
  const [actionName, setActionName] = useState("");
  const [actionResult, setActionResult] = useState("");
  const [operations, setOperations] = useState<OperationsStatus | null>(null);
  const [operationsError, setOperationsError] = useState("");
  const [operationsVersion, setOperationsVersion] = useState(0);

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

  const runControl = useCallback(async (action: "abort" | "resume") => {
    if (!task) return;
    setActionName(action);
    setActionError("");
    setActionResult("");
    try {
      const response = await fetch("/api/hitl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, task_id: task.task_id }),
      });
      const body = await response.json().catch(() => ({})) as { error?: string; detail?: string };
      if (!response.ok) {
        const detail = typeof body.detail === "string" ? body.detail : body.error;
        throw new Error(detail || `Task control returned HTTP ${response.status}`);
      }
      setActionResult(
        action === "resume"
          ? "Resume requested. The task will enter the queue when capacity is available."
          : "Cancellation requested. The task will stop at the next safe boundary.",
      );
      router.refresh();
    } catch (error) {
      setActionError(error instanceof Error ? error.message : "The task action failed.");
    } finally {
      setActionName("");
    }
  }, [router, task]);

  const copyTask = useCallback(async (purpose: "retry" | "duplicate") => {
    if (!task?.full_input) return;
    setActionName(purpose);
    setActionError("");
    try {
      const form = new FormData();
      form.append("task", task.full_input);
      form.append("variant", task.variant ?? "classic");
      const filesResponse = await fetch(
        `/api/tasks/${encodeURIComponent(task.task_id)}/files`,
        { cache: "no-store" },
      );
      if (filesResponse.ok) {
        const filesBody = await filesResponse.json() as {
          files?: { id: string; name: string }[];
        };
        await Promise.all((filesBody.files ?? []).map(async (file) => {
          const response = await fetch(
            `/api/tasks/${encodeURIComponent(task.task_id)}/files/${encodeURIComponent(file.id)}`,
            { cache: "no-store" },
          );
          if (!response.ok) {
            throw new Error(`Input copy returned HTTP ${response.status} for ${file.name}`);
          }
          form.append("files", await response.blob(), file.name);
        }));
      } else if (filesResponse.status !== 404) {
        throw new Error(`Input list returned HTTP ${filesResponse.status}`);
      }
      const response = await fetch("/api/submit", {
        method: "POST",
        body: form,
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
      setActionError(error instanceof Error ? error.message : "The task copy failed.");
    } finally {
      setActionName("");
    }
  }, [router, task]);

  const stage = useMemo(() => task ? currentStage(task) : "queued", [task]);
  if (!task) return null;
  const storageBytes = (task.storage?.input_bytes ?? 0) + (task.storage?.output_bytes ?? 0);
  const blocked = stage === "blocked";
  const cancellable = isLive || task.status === "pending" || task.status === "running";

  return (
    <section className="task-lifecycle" aria-label="Task lifecycle and operations">
      <div className="task-lifecycle__timeline">
        {STAGES.map((item) => {
          const state = stageState(item.id, stage);
          return (
            <div key={item.id} className={`task-lifecycle__stage task-lifecycle__stage--${state}`}>
              <span>
                {state === "done" ? <Check size={12} /> : item.id === "failed" && state === "current" ? <X size={12} /> : <Clock3 size={12} />}
              </span>
              <strong>{item.label}</strong>
            </div>
          );
        })}
      </div>

      <div className="task-lifecycle__operations">
        <span><small>Queue</small>{operations ? `${operations.queued}/${operations.queueCapacity}` : "…"}</span>
        <span><small>Provider</small>{operations?.providerReady ? "Healthy" : "Unavailable"}</span>
        <span><small>Agents</small>{operations ? `${operations.agentsOnline}/${operations.agentsTotal}` : "…"}</span>
        <span><small>Model</small>{task.model || "Selecting"}</span>
        <span><small>Tokens</small>{(cost?.total_tokens ?? 0).toLocaleString()}</span>
        <span><small>Cost</small>${(cost?.total_cost ?? 0).toFixed(4)}</span>
        <span><small>Storage</small>{formatBytes(storageBytes)}</span>
      </div>

      <div className="task-lifecycle__actions">
        {cancellable && !blocked && !actionResult ? (
          <button type="button" onClick={() => void runControl("abort")} disabled={!!actionName}>
            <Ban size={14} /> {actionName === "abort" ? "Cancelling…" : "Cancel"}
          </button>
        ) : null}
        {blocked && !actionResult ? (
          <button type="button" onClick={() => void runControl("resume")} disabled={!!actionName}>
            {task.run_state === "paused" ? <Play size={14} /> : <PauseCircle size={14} />}
            {actionName === "resume" ? "Resuming…" : "Resume"}
          </button>
        ) : null}
        <button type="button" onClick={() => void copyTask("duplicate")} disabled={!!actionName || !task.full_input}>
          <Copy size={14} /> {actionName === "duplicate" ? "Duplicating…" : "Duplicate"}
        </button>
      </div>

      {task.status === "failed" && task.error_message ? (
        <ActionableError
          component="Task execution"
          cause={task.error_message}
          timestamp={task.completed_at || task.last_heartbeat_at || task.created_at}
          onRetry={() => void copyTask("retry")}
          compact
        />
      ) : null}
      {actionError ? (
        <ActionableError component="Task action" cause={actionError} compact />
      ) : null}
      {actionResult ? (
        <div className="task-lifecycle__success" role="status">{actionResult}</div>
      ) : null}
      {operationsError ? (
        <ActionableError
          component="Operations status"
          cause={operationsError}
          onRetry={() => setOperationsVersion((value) => value + 1)}
          compact
        />
      ) : null}
    </section>
  );
}
