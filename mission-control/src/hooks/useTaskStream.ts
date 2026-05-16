"use client";

/**
 * useTaskStream — SSE hook for real-time task data.
 *
 * Connects to `/api/stream/task/{taskId}` and accumulates events into
 * a TaskStreamData object. For completed tasks (no active SSE), it
 * falls back to a REST fetch of the full task state.
 *
 * Handles field-name mapping between daemon and frontend shapes
 * (agent_role→agent, created_at→timestamp, ts→timestamp, etc.).
 */

import { useState, useEffect, useRef, useCallback } from "react";

// ── Types ─────────────────────────────────────────────────────────────

export type TaskStatus = "pending" | "running" | "completed" | "failed";

export interface SubTask {
  id: string;
  label: string;
  status: TaskStatus;
  agent: "planner" | "executor" | "auditor";
  depends_on: string[];
  result?: string;
  error?: string;
  started_at?: string;
  completed_at?: string;
}

/** Top-level task (used by DAGVisualizer to render the execution graph). */
export interface Task {
  id: string;
  label: string;
  status: TaskStatus;
  sub_tasks: SubTask[];
  created_at: string;
  updated_at: string;
}

export interface DebateEntry {
  id: string;
  agent_role: string;
  content: string;
  timestamp: string;
}

export interface LogEntry {
  id: string;
  agent_role: string;
  level: string;
  message: string;
  timestamp: string;
}

export interface CostData {
  total_cost: number;
  total_tokens: number;
  by_model: Record<string, { cost: number; tokens: number }>;
}

export interface TaskMeta {
  task_id: string;
  label: string;
  status: TaskStatus;
  complexity?: string;
  model?: string;
  created_at: string;
  completed_at?: string;
  duration_ms?: number;
}

export interface TaskStreamData {
  phase: string | null;
  subTasks: SubTask[];
  debates: DebateEntry[];
  logs: LogEntry[];
  cost: CostData | null;
  result: string | null;
  error: string | null;
  isLive: boolean;
  taskMeta: TaskMeta | null;
}

// ── Empty / initial state ─────────────────────────────────────────────

const INITIAL_STREAM_DATA: TaskStreamData = {
  phase: null,
  subTasks: [],
  debates: [],
  logs: [],
  cost: null,
  result: null,
  error: null,
  isLive: false,
  taskMeta: null,
};

// ── Field mapping helpers ─────────────────────────────────────────────

/** Map daemon sub-task shape (agent_role) → frontend SubTask (agent). */
function mapSubTask(raw: Record<string, unknown>): SubTask {
  return {
    id: raw.id as string,
    label: (raw.label as string) ?? "",
    status: (raw.status as TaskStatus) ?? "pending",
    agent: ((raw.agent ?? raw.agent_role) as SubTask["agent"]) ?? "planner",
    depends_on: (raw.depends_on as string[]) ?? [],
    result: raw.result as string | undefined,
    error: raw.error as string | undefined,
    started_at: raw.started_at as string | undefined,
    completed_at: raw.completed_at as string | undefined,
  };
}

/** Map daemon debate shape (created_at) → frontend DebateEntry (timestamp). */
function mapDebate(raw: Record<string, unknown>, index: number): DebateEntry {
  return {
    id: (raw.id as string) ?? `debate-${index}`,
    agent_role: (raw.agent_role as string) ?? "unknown",
    content: (raw.content as string) ?? "",
    timestamp: ((raw.timestamp ?? raw.created_at) as string) ?? new Date().toISOString(),
  };
}

/** Map daemon task → frontend TaskMeta. */
function mapTaskMeta(task: Record<string, unknown>): TaskMeta {
  return {
    task_id: (task.id ?? task.task_id) as string,
    label: (task.label as string) ?? "",
    status: (task.status as TaskStatus) ?? "pending",
    complexity: task.complexity as string | undefined,
    model: (task.model ?? task.model_used) as string | undefined,
    created_at: (task.created_at as string) ?? "",
    completed_at: task.completed_at as string | undefined,
    duration_ms: task.duration_ms as number | undefined,
  };
}

/** Map daemon log event (ts) → frontend LogEntry (timestamp). */
function mapLog(raw: Record<string, unknown>, index: number): LogEntry {
  return {
    id: (raw.id as string) ?? `log-${index}`,
    agent_role: (raw.agent_role as string) ?? "daemon",
    level: (raw.level as string) ?? "info",
    message: (raw.message as string) ?? "",
    timestamp: ((raw.timestamp ?? raw.ts) as string) ?? new Date().toISOString(),
  };
}

// ── Hook ──────────────────────────────────────────────────────────────

export function useTaskStream(taskId: string): TaskStreamData {
  const [data, setData] = useState<TaskStreamData>(INITIAL_STREAM_DATA);
  const eventSourceRef = useRef<EventSource | null>(null);

  // REST fallback for completed/failed tasks (SSE closes immediately)
  const fetchRestFallback = useCallback(async () => {
    try {
      // Fetch task detail + cost in parallel
      const [taskRes, costRes] = await Promise.all([
        fetch(`/api/tasks/${taskId}`, { cache: "no-store" }),
        fetch(`/api/tasks/${taskId}/cost`, { cache: "no-store" }).catch(() => null),
      ]);

      if (!taskRes.ok) return;
      const json = await taskRes.json();
      const task = json.task;
      const subTasks = (json.sub_tasks ?? []).map(mapSubTask);

      // Map cost data if available
      let costData: CostData | null = null;
      if (costRes?.ok) {
        try {
          const rawCost = await costRes.json();
          const byModel: Record<string, { cost: number; tokens: number }> = {};
          for (const entry of rawCost.by_model ?? []) {
            byModel[entry.model ?? "unknown"] = {
              cost: entry.cost_usd ?? 0,
              tokens: (entry.input_tokens ?? 0) + (entry.output_tokens ?? 0),
            };
          }
          costData = {
            total_cost: rawCost.total_cost_usd ?? 0,
            total_tokens: rawCost.total_tokens ?? 0,
            by_model: byModel,
          };
        } catch {
          // Cost data is non-critical
        }
      }

      setData((prev) => ({
        ...prev,
        taskMeta: task ? mapTaskMeta(task) : prev.taskMeta,
        subTasks: subTasks.length > 0 ? subTasks : prev.subTasks,
        result: task?.result_summary ?? prev.result,
        error: task?.error_message ?? prev.error,
        cost: costData ?? prev.cost,
        isLive: false,
      }));
    } catch {
      // Best-effort — REST fallback is non-critical
    }
  }, [taskId]);

  // Attempt to connect to the SSE stream for this task
  const connect = useCallback(() => {
    if (!taskId) return;

    // Close any existing connection
    eventSourceRef.current?.close();

    const es = new EventSource(`/api/stream/task/${taskId}`);
    eventSourceRef.current = es;

    es.addEventListener("open", () => {
      setData((prev) => ({ ...prev, isLive: true }));
    });

    // ── initial_state: daemon sends { task, sub_tasks } ──────────
    es.addEventListener("initial_state", (ev: MessageEvent) => {
      try {
        const payload = JSON.parse(ev.data);
        const task = payload.task;
        const subTasks = (payload.sub_tasks ?? []).map(mapSubTask);

        setData((prev) => ({
          ...prev,
          taskMeta: task ? mapTaskMeta(task) : prev.taskMeta,
          subTasks: subTasks.length > 0 ? subTasks : prev.subTasks,
          result: task?.result_summary ?? prev.result,
          error: task?.error_message ?? prev.error,
          isLive: true,
        }));
      } catch {
        console.error("[useTaskStream] Failed to parse initial_state");
      }
    });

    es.addEventListener("phase", (ev: MessageEvent) => {
      try {
        const payload = JSON.parse(ev.data);
        setData((prev) => ({ ...prev, phase: payload.phase }));
      } catch {}
    });

    // ── subtask: daemon sends agent_role, map to agent ───────────
    es.addEventListener("subtask", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const payload = mapSubTask(raw);
        setData((prev) => {
          const idx = prev.subTasks.findIndex((st) => st.id === payload.id);
          const updated =
            idx >= 0
              ? prev.subTasks.map((st, i) => (i === idx ? payload : st))
              : [...prev.subTasks, payload];
          return { ...prev, subTasks: updated };
        });
      } catch {}
    });

    // ── debate: daemon sends created_at, map to timestamp ────────
    es.addEventListener("debate", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          debates: [...prev.debates, mapDebate(raw, prev.debates.length)],
        }));
      } catch {}
    });

    // ── log: daemon sends ts, map to timestamp ──────────────────
    es.addEventListener("log", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          logs: [...prev.logs, mapLog(raw, prev.logs.length)],
        }));
      } catch {}
    });

    // ── cost: daemon sends per-call entries, accumulate ───────────
    es.addEventListener("cost", (ev: MessageEvent) => {
      try {
        const entry = JSON.parse(ev.data);
        setData((prev) => {
          const current = prev.cost ?? { total_cost: 0, total_tokens: 0, by_model: {} };
          const tokens = (entry.input_tokens ?? 0) + (entry.output_tokens ?? 0);
          const model: string = entry.model ?? "unknown";
          const existing = current.by_model[model] ?? { cost: 0, tokens: 0 };
          return {
            ...prev,
            cost: {
              total_cost: current.total_cost + (entry.cost_usd ?? 0),
              total_tokens: current.total_tokens + tokens,
              by_model: {
                ...current.by_model,
                [model]: {
                  cost: existing.cost + (entry.cost_usd ?? 0),
                  tokens: existing.tokens + tokens,
                },
              },
            },
          };
        });
      } catch {}
    });

    // ── complete: daemon emits event: complete (NOT task-completed) ─
    // For completed tasks the daemon sends the full task dict as the payload.
    // We must build taskMeta from it (prev.taskMeta is likely null for
    // history tasks whose SSE immediately emits complete then closes).
    es.addEventListener("complete", (ev: MessageEvent) => {
      try {
        const payload = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          isLive: false,
          result: payload.result_summary ?? prev.result,
          taskMeta: prev.taskMeta
            ? { ...prev.taskMeta, status: "completed" }
            : mapTaskMeta({ ...payload, status: "completed" }),
        }));
      } catch {}
      es.close();
      // Hydrate remaining data (cost, debates, logs) via REST
      void fetchRestFallback();
    });

    // ── error: daemon emits event: error (NOT task-failed) ─────────
    // Named SSE event "error" is distinct from the native EventSource
    // error handler. We guard on ev.data to disambiguate.
    es.addEventListener("error", (ev: MessageEvent) => {
      // Native EventSource errors have no data property
      if (!ev.data) {
        // Auto-reconnect unless explicitly closed
        if (es.readyState === EventSource.CLOSED) {
          // Stream closed — use REST fallback to hydrate full task state
          void fetchRestFallback();
          setData((prev) => ({ ...prev, isLive: false }));
        }
        return;
      }

      // Named "error" event from daemon with payload (full task dict)
      try {
        const payload = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          isLive: false,
          error: payload.error_message ?? payload.error ?? "Task failed",
          taskMeta: prev.taskMeta
            ? { ...prev.taskMeta, status: "failed" }
            : mapTaskMeta({ ...payload, status: "failed" }),
        }));
      } catch {}
      es.close();
      // Hydrate remaining data via REST
      void fetchRestFallback();
    });
  }, [taskId, fetchRestFallback]);

  // Connect on mount, disconnect on unmount
  useEffect(() => {
    connect();
    return () => {
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
    };
  }, [connect]);

  return data;
}
