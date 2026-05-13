import { create } from "zustand";

// ── bMAS Planner Schema Types ─────────────────────────────────────────

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

export interface Task {
  id: string;
  label: string;
  status: TaskStatus;
  sub_tasks: SubTask[];
  created_at: string;
  updated_at: string;
}

export interface DaemonState {
  phase: string;
  iteration: number;
  paused: boolean;
  tasks: Record<string, Task>;
  agents: {
    planner: AgentStatus;
    executor: AgentStatus;
    auditor: AgentStatus;
  };
}

export interface AgentStatus {
  alive: boolean;
  last_heartbeat: string;
  current_task?: string;
}

// ── Zustand Store ─────────────────────────────────────────────────────

interface BlackboardStore {
  /** The latest daemon state snapshot (null until first successful poll). */
  state: DaemonState | null;

  /** Whether the last poll attempt failed. */
  error: string | null;

  /** True while the very first fetch is in-flight. */
  loading: boolean;

  /** Timestamp (ms) of the last successful poll. */
  lastUpdated: number;

  /** Start the 2-second polling loop. Returns a cleanup function. */
  startPolling: () => () => void;
}

export const useBlackboard = create<BlackboardStore>((set) => ({
  state: null,
  error: null,
  loading: true,
  lastUpdated: 0,

  startPolling: () => {
    let alive = true;

    const poll = async () => {
      try {
        const res = await fetch("/api/state", { cache: "no-store" });

        if (!res.ok) {
          const body = (await res.json().catch(() => ({}))) as Record<
            string,
            unknown
          >;
          set({
            error:
              typeof body.error === "string"
                ? body.error
                : `HTTP ${res.status}`,
            loading: false,
          });
          return;
        }

        const data = (await res.json()) as DaemonState;
        set({
          state: data,
          error: null,
          loading: false,
          lastUpdated: Date.now(),
        });
      } catch (err) {
        set({
          error:
            err instanceof Error ? err.message : "Network error",
          loading: false,
        });
      }
    };

    // Immediately fire the first poll.
    void poll();

    const interval = setInterval(() => {
      if (alive) void poll();
    }, 2_000);

    // Return cleanup function for useEffect.
    return () => {
      alive = false;
      clearInterval(interval);
    };
  },
}));

// ── Derived selectors (for component-level subscriptions) ─────────────
// These selectors must return stable references to avoid infinite loops
// with React 19's useSyncExternalStore. We cache the last result and
// only create a new array when the underlying tasks object changes.

const EMPTY_TASKS: Task[] = [];
let _cachedTasks: Task[] = EMPTY_TASKS;
let _cachedTasksRef: Record<string, Task> | null = null;

/** Select all tasks as a flat array (stable reference). */
export const selectTasks = (store: BlackboardStore): Task[] => {
  const tasks = store.state?.tasks ?? null;
  if (tasks === null) return EMPTY_TASKS;
  if (tasks !== _cachedTasksRef) {
    _cachedTasksRef = tasks;
    _cachedTasks = Object.values(tasks);
  }
  return _cachedTasks;
};

const EMPTY_SUBTASKS: SubTask[] = [];
let _cachedSubTasks: SubTask[] = EMPTY_SUBTASKS;
let _cachedSubTasksRef: Record<string, Task> | null = null;

/** Select all sub-tasks across every task (stable reference). */
export const selectAllSubTasks = (store: BlackboardStore): SubTask[] => {
  const tasks = store.state?.tasks ?? null;
  if (tasks === null) return EMPTY_SUBTASKS;
  if (tasks !== _cachedSubTasksRef) {
    _cachedSubTasksRef = tasks;
    _cachedSubTasks = Object.values(tasks).flatMap((t) => t.sub_tasks);
  }
  return _cachedSubTasks;
};

/** Select a single task by ID. */
export const selectTaskById =
  (taskId: string) =>
  (store: BlackboardStore): Task | undefined =>
    store.state?.tasks[taskId];
