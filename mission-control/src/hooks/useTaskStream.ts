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
import {
  mapSubTask,
  mapDebate,
  mapLog,
  mapTaskMeta,
} from "@/lib/mappers";

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

// ── Phase 4 Types (doc 08/09) ─────────────────────────────────────────

export interface BoardEntry {
  id: string;
  type: string;
  title: string;
  body: string;
  author: string;
  refs: string[];
  confidence: number;
  salience: number;
  seq: number;
  created_at: string;
  round?: number;
  status?: string;
}

export interface TurnRecord {
  turn_id: string;
  task_id: string;
  actor: string;
  round_no: number;
  phase: string;
  status: string;
  started_at: string;
  ended_at?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  model?: string;
}

export interface TraceEvent {
  id: string;
  turn_id: string;
  actor: string;
  type: string;
  content: string;
  seq: number;
  timestamp: string;
  run_id?: string;
}

export interface RejectedEntry {
  entry_id: string;
  actor: string;
  reason: string;
  timestamp: string;
}

export interface CostData {
  total_cost: number;
  total_tokens: number;
  by_model: Record<string, { cost: number; tokens: number }>;
}

// Phase 5: HITL Types (doc 05 §6, doc 12 §5.1)

export interface ApprovalRequest {
  turn_id: string;
  actor: string;
  run_id: string;
  description: string;
  timestamp: string;
}

export interface BudgetState {
  spent: number;
  ceiling: number;
  percentage: number;
}

export interface CoordinatorNarration {
  round: number;
  selected: string[];
  rationale: string | null;
  source: string;
  timestamp: string;
}

export interface TaskMeta {
  task_id: string;
  label: string;
  status: TaskStatus;
  complexity?: string;
  model?: string;
  variant?: string;
  created_at: string;
  completed_at?: string;
  duration_ms?: number;
}

export interface ConsensusState {
  signal: number;
  decider_state: string;
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
  // Phase 4 — board, trace, turns
  boardEntries: BoardEntry[];
  removedEntryIds: string[];
  consensus: ConsensusState | null;
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  traceEvents: TraceEvent[];
  rejectedEntries: RejectedEntry[];
  // Phase 5 — HITL, budget, narration
  approvalRequests: ApprovalRequest[];
  isPaused: boolean;
  budgetState: BudgetState | null;
  coordinatorNarrations: CoordinatorNarration[];
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
  boardEntries: [],
  removedEntryIds: [],
  consensus: null,
  activeTurns: [],
  completedTurns: [],
  traceEvents: [],
  rejectedEntries: [],
  approvalRequests: [],
  isPaused: false,
  budgetState: null,
  coordinatorNarrations: [],
};

// Field mapping helpers are imported from @/lib/mappers.
// They are pure functions that convert daemon API shapes to frontend types.
// Tests: src/lib/__tests__/mappers.test.ts


// ── Hook ──────────────────────────────────────────────────────────────

export function useTaskStream(taskId: string): TaskStreamData {
  const [data, setData] = useState<TaskStreamData>(INITIAL_STREAM_DATA);
  const eventSourceRef = useRef<EventSource | null>(null);

  // rAF batching buffers for high-frequency events (doc 09 §8, doc 13 §5)
  const boardBufRef = useRef<BoardEntry[]>([]);
  const traceBufRef = useRef<TraceEvent[]>([]);
  const rafRef = useRef<number | null>(null);

  const flushBuffers = useCallback(() => {
    const boardBatch = boardBufRef.current;
    const traceBatch = traceBufRef.current;
    if (boardBatch.length === 0 && traceBatch.length === 0) return;
    boardBufRef.current = [];
    traceBufRef.current = [];
    setData((prev) => ({
      ...prev,
      ...(boardBatch.length > 0
        ? { boardEntries: [...prev.boardEntries, ...boardBatch] }
        : {}),
      ...(traceBatch.length > 0
        ? { traceEvents: [...prev.traceEvents, ...traceBatch] }
        : {}),
    }));
  }, []);

  const scheduleFlush = useCallback(() => {
    if (rafRef.current === null) {
      rafRef.current = requestAnimationFrame(() => {
        rafRef.current = null;
        flushBuffers();
      });
    }
  }, [flushBuffers]);

  // REST fallback for completed/failed tasks (SSE closes immediately)
  const fetchRestFallback = useCallback(async () => {
    try {
      // Fetch task detail + cost + board + turns in parallel
      const [taskRes, costRes, boardRes, turnsRes] = await Promise.all([
        fetch(`/api/tasks/${taskId}`, { cache: "no-store" }),
        fetch(`/api/tasks/${taskId}/cost`, { cache: "no-store" }).catch(() => null),
        fetch(`/api/tasks/${taskId}/board`, { cache: "no-store" }).catch(() => null),
        fetch(`/api/tasks/${taskId}/turns`, { cache: "no-store" }).catch(() => null),
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

      // Map board entries for completed tasks (Bug 1 fix)
      let hydratedBoard: BoardEntry[] = [];
      if (boardRes?.ok) {
        try {
          const boardData = await boardRes.json();
          const rawEntries = Array.isArray(boardData) ? boardData : boardData.entries ?? [];
          hydratedBoard = rawEntries.map((raw: Record<string, unknown>, idx: number) => ({
            id: (raw.id ?? raw.entry_id ?? `e-${idx}`) as string,
            type: (raw.type ?? raw.entry_type ?? "finding") as string,
            title: (raw.title ?? "") as string,
            body: (raw.body ?? raw.content ?? "") as string,
            author: (raw.author ?? raw.actor ?? "unknown") as string,
            refs: (raw.refs ?? []) as string[],
            confidence: (raw.confidence ?? 0) as number,
            salience: (raw.salience ?? 0) as number,
            seq: (raw.seq ?? idx) as number,
            created_at: (raw.created_at ?? "") as string,
          }));
        } catch {
          // Board data is non-critical
        }
      }

      // Map completed turns for the trace timeline
      let hydratedTurns: TurnRecord[] = [];
      if (turnsRes?.ok) {
        try {
          const turnsData = await turnsRes.json();
          const rawTurns = Array.isArray(turnsData) ? turnsData : turnsData.turns ?? [];
          hydratedTurns = rawTurns.map((raw: Record<string, unknown>) => ({
            turn_id: (raw.turn_id ?? raw.id ?? "") as string,
            actor: (raw.actor ?? raw.role ?? "unknown") as string,
            round_no: (raw.round_no ?? raw.round ?? 0) as number,
            phase: (raw.phase ?? "completed") as string,
            status: (raw.status ?? "completed") as string,
            started_at: (raw.started_at ?? raw.created_at ?? "") as string,
            ended_at: (raw.ended_at ?? raw.completed_at) as string | undefined,
            tokens_in: (raw.tokens_in ?? raw.input_tokens) as number | undefined,
            tokens_out: (raw.tokens_out ?? raw.output_tokens) as number | undefined,
            cost_usd: raw.cost_usd as number | undefined,
            model: raw.model as string | undefined,
          }));
        } catch {
          // Turn data is non-critical
        }
      }

      setData((prev) => ({
        ...prev,
        taskMeta: task ? mapTaskMeta(task) : prev.taskMeta,
        subTasks: subTasks.length > 0 ? subTasks : prev.subTasks,
        result: task?.result_summary ?? prev.result,
        error: task?.error_message ?? prev.error,
        cost: costData ?? prev.cost,
        boardEntries: hydratedBoard.length > 0 ? hydratedBoard : prev.boardEntries,
        completedTurns: hydratedTurns.length > 0 ? hydratedTurns : prev.completedTurns,
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

    // ── Phase 4: board_entry — buffer + rAF flush ───────────────────
    es.addEventListener("board_entry", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const entry: BoardEntry = {
          id: raw.id ?? raw.entry_id ?? `entry-${Date.now()}`,
          type: raw.type ?? raw.entry_type ?? "finding",
          title: raw.title ?? "",
          body: raw.body ?? raw.content ?? "",
          author: raw.author ?? raw.actor ?? "unknown",
          refs: raw.refs ?? [],
          confidence: raw.confidence ?? 0,
          salience: raw.salience ?? 0,
          seq: raw.seq ?? 0,
          created_at: raw.created_at ?? new Date().toISOString(),
        };
        boardBufRef.current.push(entry);
        scheduleFlush();
      } catch {}
    });

    // ── Phase 4: entry_removed ──────────────────────────────────────
    es.addEventListener("entry_removed", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const entryId = raw.entry_id ?? raw.id;
        if (entryId) {
          setData((prev) => ({
            ...prev,
            removedEntryIds: [...prev.removedEntryIds, entryId],
          }));
        }
      } catch {}
    });

    // ── Phase 4: consensus ──────────────────────────────────────────
    es.addEventListener("consensus", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          consensus: {
            signal: raw.signal ?? raw.convergence ?? 0,
            decider_state: raw.decider_state ?? raw.state ?? "evaluating",
          },
        }));
      } catch {}
    });

    // ── Phase 4: turn_start ─────────────────────────────────────────
    es.addEventListener("turn_start", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const turn: TurnRecord = {
          turn_id: raw.turn_id ?? `turn-${Date.now()}`,
          task_id: raw.task_id ?? taskId,
          actor: raw.actor ?? raw.agent_role ?? "unknown",
          round_no: raw.round_no ?? raw.round ?? 0,
          phase: raw.phase ?? "active",
          status: "active",
          started_at: raw.started_at ?? new Date().toISOString(),
          model: raw.model as string | undefined,
        };
        setData((prev) => ({
          ...prev,
          activeTurns: [...prev.activeTurns, turn],
        }));
      } catch {}
    });

    // ── Phase 4: turn_end ───────────────────────────────────────────
    es.addEventListener("turn_end", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const turnId = raw.turn_id;
        setData((prev) => {
          const finished = prev.activeTurns.find((t) => t.turn_id === turnId);
          return {
            ...prev,
            activeTurns: prev.activeTurns.filter((t) => t.turn_id !== turnId),
            completedTurns: finished
              ? [
                  ...prev.completedTurns,
                  {
                    ...finished,
                    status: raw.status ?? "completed",
                    ended_at: raw.ended_at ?? new Date().toISOString(),
                    tokens_in: raw.tokens_in ?? raw.input_tokens,
                    tokens_out: raw.tokens_out ?? raw.output_tokens,
                    cost_usd: raw.cost_usd,
                  },
                ]
              : prev.completedTurns,
          };
        });
      } catch {}
    });

    // ── Phase 4: trace — buffer + rAF flush ─────────────────────────
    es.addEventListener("trace", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const trace: TraceEvent = {
          id: raw.id ?? `trace-${Date.now()}-${Math.random().toString(36).slice(2, 6)}`,
          turn_id: raw.turn_id ?? "",
          actor: raw.actor ?? raw.agent_role ?? "unknown",
          type: raw.type ?? raw.trace_type ?? "reasoning",
          content: raw.content ?? raw.message ?? "",
          seq: raw.seq ?? 0,
          timestamp: raw.timestamp ?? raw.ts ?? new Date().toISOString(),
        };
        traceBufRef.current.push(trace);
        scheduleFlush();
      } catch {}
    });

    // ── Phase 4: entry_rejected ─────────────────────────────────────
    es.addEventListener("entry_rejected", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          rejectedEntries: [
            ...prev.rejectedEntries,
            {
              entry_id: raw.entry_id ?? raw.id ?? "",
              actor: raw.actor ?? "unknown",
              reason: raw.reason ?? "Unknown reason",
              timestamp: raw.timestamp ?? new Date().toISOString(),
            },
          ],
        }));
      } catch {}
    });

    // ── Phase 5: approval_request ────────────────────────────────────
    es.addEventListener("approval_request", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const req: ApprovalRequest = {
          turn_id: raw.turn_id ?? "",
          actor: raw.actor ?? "unknown",
          run_id: raw.run_id ?? "",
          description: raw.description ?? "",
          timestamp: raw.timestamp ?? new Date().toISOString(),
        };
        setData((prev) => ({
          ...prev,
          approvalRequests: [...prev.approvalRequests, req],
        }));
      } catch {}
    });

    // ── Phase 5: paused / resumed ───────────────────────────────────
    es.addEventListener("paused", () => {
      setData((prev) => ({ ...prev, isPaused: true }));
    });

    es.addEventListener("resumed", () => {
      setData((prev) => ({ ...prev, isPaused: false }));
    });

    // ── Phase 5: budget ─────────────────────────────────────────────
    es.addEventListener("budget", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          budgetState: {
            spent: raw.spent ?? 0,
            ceiling: raw.ceiling ?? 0,
            percentage: raw.percentage ?? 0,
          },
        }));
      } catch {}
    });

    // ── Phase 5: coordinator_narration ───────────────────────────────
    es.addEventListener("coordinator_narration", (ev: MessageEvent) => {
      try {
        const raw = JSON.parse(ev.data);
        const narration: CoordinatorNarration = {
          round: raw.round ?? 0,
          selected: raw.selected ?? [],
          rationale: raw.rationale ?? null,
          source: raw.source ?? "unknown",
          timestamp: new Date().toISOString(),
        };
        setData((prev) => ({
          ...prev,
          coordinatorNarrations: [...prev.coordinatorNarrations, narration],
        }));
      } catch {}
    });
  }, [taskId, fetchRestFallback, scheduleFlush]);

  // Connect on mount, disconnect on unmount
  useEffect(() => {
    connect();
    return () => {
      eventSourceRef.current?.close();
      eventSourceRef.current = null;
      if (rafRef.current !== null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, [connect]);

  return data;
}
