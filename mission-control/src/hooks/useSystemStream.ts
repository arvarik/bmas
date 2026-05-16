"use client";

/**
 * useSystemStream — SSE hook for global system health.
 *
 * Connects to `/api/stream/system` and receives daemon-status,
 * agent-health, task-started, and task-completed events.
 *
 * Replaces `useBlackboard.startPolling()` in the shell layer.
 */

import { useState, useEffect, useRef, useCallback } from "react";

// ── Types ─────────────────────────────────────────────────────────────

export interface AgentHealthEntry {
  alive: boolean;
  last_heartbeat: string;
  current_task?: string | null;
}

export interface TaskLifecycleEvent {
  type: "started" | "completed";
  task_id: string;
  label?: string;
  status?: string;
}

export interface SystemStreamData {
  /** Overall daemon health: healthy | degraded | disconnected */
  daemonStatus: "healthy" | "degraded" | "disconnected";
  /** Whether Redis is reachable */
  redisConnected: boolean;
  /** Whether SQLite is reachable */
  sqliteConnected: boolean;
  /** Per-agent health data */
  agentHealth: Record<string, AgentHealthEntry>;
  /** The most recent task lifecycle event (changes on each event) */
  latestTaskEvent: TaskLifecycleEvent | null;
  /** Monotonic counter that increments on every lifecycle event.
   *  Use as a dependency for re-fetch triggers. */
  eventSequence: number;
}

// ── Initial state ─────────────────────────────────────────────────────

const INITIAL: SystemStreamData = {
  daemonStatus: "disconnected",
  redisConnected: false,
  sqliteConnected: false,
  agentHealth: {},
  latestTaskEvent: null,
  eventSequence: 0,
};

// ── Hook ──────────────────────────────────────────────────────────────

export function useSystemStream(): SystemStreamData {
  const [data, setData] = useState<SystemStreamData>(INITIAL);
  const esRef = useRef<EventSource | null>(null);

  const connect = useCallback(() => {
    esRef.current?.close();

    const es = new EventSource("/api/stream/system");
    esRef.current = es;

    es.addEventListener("open", () => {
      setData((prev) => ({ ...prev, daemonStatus: "healthy" }));
    });

    // daemon-status: { status, redis_connected, sqlite_connected }
    es.addEventListener("daemon-status", (ev: MessageEvent) => {
      try {
        const p = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          daemonStatus: p.status === "healthy" ? "healthy" : "degraded",
          redisConnected: !!p.redis_connected,
          sqliteConnected: !!p.sqlite_connected,
        }));
      } catch {}
    });

    // agent-health: { planner: {...}, executor: {...}, auditor: {...} }
    es.addEventListener("agent-health", (ev: MessageEvent) => {
      try {
        const p = JSON.parse(ev.data);
        setData((prev) => ({ ...prev, agentHealth: p }));
      } catch {}
    });

    // task-started: { task_id, label }
    es.addEventListener("task-started", (ev: MessageEvent) => {
      try {
        const p = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          latestTaskEvent: { type: "started", task_id: p.task_id, label: p.label },
          eventSequence: prev.eventSequence + 1,
        }));
      } catch {}
    });

    // task-completed: { task_id, status, label }
    es.addEventListener("task-completed", (ev: MessageEvent) => {
      try {
        const p = JSON.parse(ev.data);
        setData((prev) => ({
          ...prev,
          latestTaskEvent: {
            type: "completed",
            task_id: p.task_id,
            label: p.label,
            status: p.status,
          },
          eventSequence: prev.eventSequence + 1,
        }));
      } catch {}
    });

    es.addEventListener("error", () => {
      // EventSource auto-reconnects. Mark disconnected only if CLOSED.
      if (es.readyState === EventSource.CLOSED) {
        setData((prev) => ({ ...prev, daemonStatus: "disconnected" }));
      }
    });
  }, []);

  useEffect(() => {
    connect();
    return () => {
      esRef.current?.close();
      esRef.current = null;
    };
  }, [connect]);

  return data;
}
