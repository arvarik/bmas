"use client";

/**
 * useSystemStream connects the shell to global daemon events.
 *
 * The EventSource `open` event confirms only the transport connection.
 * A recent `daemon-status` event confirms the daemon state.
 */

import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";

export type SystemConnectionState =
  | "connecting"
  | "ready"
  | "degraded"
  | "reconnecting"
  | "disconnected"
  | "offline";

export interface AgentHealthEntry {
  alive: boolean;
  ready?: boolean;
  last_heartbeat: string;
  current_task?: string | null;
}

export interface TaskLifecycleEvent {
  type: "started" | "completed";
  task_id: string;
  label?: string;
  status?: string;
}

export interface SystemDependencyIssue {
  id: string;
  label: string;
  detail: string;
  affectedFeatures: string[];
  remediation: string;
}

export interface SystemStreamData {
  connectionState: SystemConnectionState;
  /** Compatibility state for consumers that still use daemon health. */
  daemonStatus: "healthy" | "degraded" | "disconnected";
  redisConnected: boolean;
  sqliteConnected: boolean;
  agentHealth: Record<string, AgentHealthEntry>;
  latestTaskEvent: TaskLifecycleEvent | null;
  eventSequence: number;
  lastSuccessfulEventAt: string | null;
  isStale: boolean;
  failedDependencies: SystemDependencyIssue[];
  affectedFeatures: string[];
  reconnect: () => void;
}

type StreamTransportState = "connecting" | "open" | "closed";
type DaemonReport = "healthy" | "degraded" | null;

export const SYSTEM_EVENT_STALE_AFTER_MS = 20_000;
export const SYSTEM_STATUS_WAIT_MS = 10_000;

interface StateDeadlineInput {
  daemonReport: DaemonReport;
  awaitingFreshDaemonStatus: boolean;
  lastDaemonStatusAtMs: number | null;
  transportOpenedAtMs: number | null;
  now: number;
}

export function nextSystemStateDeadline(input: StateDeadlineInput): number | null {
  const deadlines: number[] = [];
  if (input.lastDaemonStatusAtMs !== null) {
    deadlines.push(input.lastDaemonStatusAtMs + SYSTEM_EVENT_STALE_AFTER_MS);
  }
  if (
    (input.daemonReport === null || input.awaitingFreshDaemonStatus)
    && input.transportOpenedAtMs !== null
  ) {
    deadlines.push(input.transportOpenedAtMs + SYSTEM_STATUS_WAIT_MS);
  }
  const futureDeadlines = deadlines.filter((deadline) => deadline > input.now);
  return futureDeadlines.length > 0 ? Math.min(...futureDeadlines) : null;
}

interface DerivedStateInput {
  browserOnline: boolean;
  transportState: StreamTransportState;
  connectionFailed: boolean;
  awaitingFreshDaemonStatus: boolean;
  daemonReport: DaemonReport;
  redisConnected: boolean;
  sqliteConnected: boolean;
  agentHealth: Record<string, AgentHealthEntry>;
  lastSuccessfulEventAtMs: number | null;
  lastDaemonStatusAtMs: number | null;
  transportOpenedAtMs: number | null;
  now: number;
}

interface DerivedState {
  connectionState: SystemConnectionState;
  daemonStatus: SystemStreamData["daemonStatus"];
  isStale: boolean;
  failedDependencies: SystemDependencyIssue[];
  affectedFeatures: string[];
}

const DAEMON_ISSUE: SystemDependencyIssue = {
  id: "daemon",
  label: "Daemon connection",
  detail: "Mission Control cannot reach the coordination daemon.",
  affectedFeatures: ["Live data", "Task submission", "Operator actions"],
  remediation: "Run `docker compose ps`, then inspect the daemon logs.",
};

const OFFLINE_ISSUE: SystemDependencyIssue = {
  id: "browser-network",
  label: "Browser network",
  detail: "This browser reports that its network connection is offline.",
  affectedFeatures: ["Server data", "Task submission", "Operator actions"],
  remediation: "Restore the network connection, then retry the live connection.",
};

const STREAM_ISSUE: SystemDependencyIssue = {
  id: "event-stream",
  label: "System event stream",
  detail: "Mission Control has not received a recent system health event.",
  affectedFeatures: ["Live status", "Task activity", "Automatic refresh"],
  remediation: "Retry the connection. Inspect the daemon logs if the stream stays stale.",
};

const RECONNECTING_ISSUE: SystemDependencyIssue = {
  id: "event-stream",
  label: "System event stream",
  detail: "The live event connection was interrupted.",
  affectedFeatures: ["Live status", "Task activity", "Automatic refresh"],
  remediation: "Wait for the automatic retry, or retry the connection now.",
};

function dependencyIssues(input: DerivedStateInput, stale: boolean): SystemDependencyIssue[] {
  if (!input.browserOnline) return [OFFLINE_ISSUE];

  if (input.transportState === "closed" || input.connectionFailed) {
    if (input.lastSuccessfulEventAtMs === null || stale) return [DAEMON_ISSUE];
  }

  const issues: SystemDependencyIssue[] = [];

  if (stale) issues.push(STREAM_ISSUE);
  if (input.daemonReport === "degraded") {
    if (!input.redisConnected) {
      issues.push({
        id: "redis",
        label: "Redis",
        detail: "The live coordination and lock service is unavailable.",
        affectedFeatures: ["Live task updates", "Task controls", "Agent coordination"],
        remediation: "Run `docker compose logs redis` and restore the Redis service.",
      });
    }
    if (!input.sqliteConnected) {
      issues.push({
        id: "sqlite",
        label: "SQLite",
        detail: "The durable task database is unavailable.",
        affectedFeatures: ["Task history", "Event replay", "Task recovery"],
        remediation: "Run `docker compose logs daemon` and verify the database volume.",
      });
    }
    if (input.redisConnected && input.sqliteConnected) {
      issues.push({
        id: "daemon-dependency",
        label: "Daemon dependency",
        detail: "The daemon reports a degraded dependency.",
        affectedFeatures: ["Task execution"],
        remediation: "Open Infrastructure and inspect the detailed health checks.",
      });
    }
  }

  const unavailableAgents = Object.entries(input.agentHealth)
    .filter(([, health]) => !health.alive || health.ready === false)
    .map(([role]) => role.replaceAll("_", " "));
  if (unavailableAgents.length > 0) {
    issues.push({
      id: "agents",
      label: "Execution agents",
      detail: `Unavailable: ${unavailableAgents.join(", ")}.`,
      affectedFeatures: ["Task execution", "Approvals", "Run steering"],
      remediation: "Run `docker compose logs agent` and restore the unavailable agents.",
    });
  }

  return issues;
}

/** Derive one user-facing state from transport, daemon, and dependency data. */
export function deriveSystemState(input: DerivedStateInput): DerivedState {
  if (!input.browserOnline) {
    return {
      connectionState: "offline",
      daemonStatus: "disconnected",
      isStale: true,
      failedDependencies: [OFFLINE_ISSUE],
      affectedFeatures: OFFLINE_ISSUE.affectedFeatures,
    };
  }

  const daemonStatusAge = input.lastDaemonStatusAtMs === null
    ? null
    : Math.max(0, input.now - input.lastDaemonStatusAtMs);
  const isStale = input.daemonReport !== null
    && (daemonStatusAge === null || daemonStatusAge >= SYSTEM_EVENT_STALE_AFTER_MS);
  const statusWaitTimedOut = input.transportOpenedAtMs !== null
    && input.now - input.transportOpenedAtMs >= SYSTEM_STATUS_WAIT_MS;

  let connectionState: SystemConnectionState;
  if (input.transportState === "closed") {
    connectionState = "disconnected";
  } else if (input.connectionFailed) {
    connectionState = input.lastSuccessfulEventAtMs !== null && !isStale
      ? "reconnecting"
      : "disconnected";
  } else if (input.awaitingFreshDaemonStatus) {
    connectionState = statusWaitTimedOut
      ? input.transportState === "open" ? "degraded" : "disconnected"
      : "reconnecting";
  } else if (input.transportState === "connecting") {
    connectionState = input.daemonReport === null
      ? statusWaitTimedOut ? "disconnected" : "connecting"
      : "reconnecting";
  } else if (input.daemonReport === null) {
    const waitedForStatus = input.transportOpenedAtMs !== null
      && input.now - input.transportOpenedAtMs >= SYSTEM_STATUS_WAIT_MS;
    connectionState = waitedForStatus ? "degraded" : "connecting";
  } else if (isStale) {
    connectionState = "degraded";
  } else {
    const agentsDegraded = Object.values(input.agentHealth)
      .some((health) => !health.alive || health.ready === false);
    connectionState = input.daemonReport === "healthy" && !agentsDegraded
      ? "ready"
      : "degraded";
  }

  let issues = dependencyIssues(input, isStale);
  if (connectionState === "reconnecting" && !issues.some((issue) => issue.id === "event-stream")) {
    issues = [RECONNECTING_ISSUE, ...issues];
  }
  if (connectionState === "disconnected" && !issues.some((issue) => issue.id === "daemon")) {
    issues = [DAEMON_ISSUE, ...issues];
  }
  if (
    connectionState === "degraded"
    && (input.daemonReport === null || input.awaitingFreshDaemonStatus)
    && !issues.some((issue) => issue.id === "event-stream")
  ) {
    issues = [STREAM_ISSUE];
  }
  const affectedFeatures = Array.from(
    new Set(issues.flatMap((issue) => issue.affectedFeatures)),
  );

  return {
    connectionState,
    daemonStatus: connectionState === "ready"
      ? "healthy"
      : connectionState === "degraded"
        ? "degraded"
        : "disconnected",
    isStale,
    failedDependencies: issues,
    affectedFeatures,
  };
}

interface SystemSnapshot {
  connectionState: SystemConnectionState;
  daemonStatus: SystemStreamData["daemonStatus"];
  redisConnected: boolean;
  sqliteConnected: boolean;
  agentHealth: Record<string, AgentHealthEntry>;
  latestTaskEvent: TaskLifecycleEvent | null;
  eventSequence: number;
  lastSuccessfulEventAt: string | null;
  isStale: boolean;
  failedDependencies: SystemDependencyIssue[];
  affectedFeatures: string[];
}

const INITIAL: SystemSnapshot = {
  connectionState: "connecting",
  daemonStatus: "disconnected",
  redisConnected: false,
  sqliteConnected: false,
  agentHealth: {},
  latestTaskEvent: null,
  eventSequence: 0,
  lastSuccessfulEventAt: null,
  isStale: false,
  failedDependencies: [],
  affectedFeatures: [],
};

function subscribeToNetworkStatus(callback: () => void): () => void {
  window.addEventListener("online", callback);
  window.addEventListener("offline", callback);
  return () => {
    window.removeEventListener("online", callback);
    window.removeEventListener("offline", callback);
  };
}

function getOnlineSnapshot(): boolean {
  return navigator.onLine;
}

function getServerOnlineSnapshot(): boolean {
  return true;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function useSystemStream(): SystemStreamData {
  const [data, setData] = useState<SystemSnapshot>(INITIAL);
  const browserOnline = useSyncExternalStore(
    subscribeToNetworkStatus,
    getOnlineSnapshot,
    getServerOnlineSnapshot,
  );
  const esRef = useRef<EventSource | null>(null);
  const transportStateRef = useRef<StreamTransportState>("connecting");
  const connectionFailedRef = useRef(false);
  const awaitingFreshDaemonStatusRef = useRef(false);
  const daemonReportRef = useRef<DaemonReport>(null);
  const lastEventAtRef = useRef<number | null>(null);
  const lastDaemonStatusAtRef = useRef<number | null>(null);
  const openedAtRef = useRef<number | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const updateData = useCallback((updater: (previous: SystemSnapshot) => SystemSnapshot) => {
    setData((previous) => {
      const next = updater(previous);
      return next;
    });
  }, []);

  const applyDerivedState = useCallback((snapshot: SystemSnapshot, now: number): SystemSnapshot => {
    const derived = deriveSystemState({
      browserOnline,
      transportState: transportStateRef.current,
      connectionFailed: connectionFailedRef.current,
      awaitingFreshDaemonStatus: awaitingFreshDaemonStatusRef.current,
      daemonReport: daemonReportRef.current,
      redisConnected: snapshot.redisConnected,
      sqliteConnected: snapshot.sqliteConnected,
      agentHealth: snapshot.agentHealth,
      lastSuccessfulEventAtMs: lastEventAtRef.current,
      lastDaemonStatusAtMs: lastDaemonStatusAtRef.current,
      transportOpenedAtMs: openedAtRef.current,
      now,
    });
    return { ...snapshot, ...derived };
  }, [browserOnline]);

  const scheduleStateCheck = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    function scheduleNextDeadline(): void {
      const now = Date.now();
      const nextDeadline = nextSystemStateDeadline({
        daemonReport: daemonReportRef.current,
        awaitingFreshDaemonStatus: awaitingFreshDaemonStatusRef.current,
        lastDaemonStatusAtMs: lastDaemonStatusAtRef.current,
        transportOpenedAtMs: openedAtRef.current,
        now,
      });
      if (nextDeadline === null) return;
      timerRef.current = setTimeout(() => {
        updateData((previous) => applyDerivedState(previous, Date.now()));
        scheduleNextDeadline();
      }, Math.max(0, nextDeadline - now + 10));
    }
    scheduleNextDeadline();
  }, [applyDerivedState, updateData]);

  const recordSuccessfulEvent = useCallback(
    (updater: (previous: SystemSnapshot) => SystemSnapshot, confirmsDaemon = false) => {
      const now = Date.now();
      lastEventAtRef.current = now;
      connectionFailedRef.current = false;
      if (confirmsDaemon) {
        awaitingFreshDaemonStatusRef.current = false;
        lastDaemonStatusAtRef.current = now;
      }
      updateData((previous) => applyDerivedState({
        ...updater(previous),
        lastSuccessfulEventAt: new Date(now).toISOString(),
        isStale: false,
      }, now));
      scheduleStateCheck();
    },
    [applyDerivedState, scheduleStateCheck, updateData],
  );

  const connect = useCallback(() => {
    esRef.current?.close();
    if (!browserOnline) return;

    transportStateRef.current = "connecting";
    connectionFailedRef.current = false;
    awaitingFreshDaemonStatusRef.current = daemonReportRef.current !== null;
    openedAtRef.current = Date.now();
    updateData((previous) => applyDerivedState(previous, Date.now()));

    const es = new EventSource("/api/stream/system");
    esRef.current = es;
    scheduleStateCheck();

    es.addEventListener("open", () => {
      transportStateRef.current = "open";
      connectionFailedRef.current = false;
      openedAtRef.current = Date.now();
      updateData((previous) => applyDerivedState(previous, Date.now()));
      scheduleStateCheck();
    });

    es.addEventListener("daemon-status", (event: MessageEvent) => {
      try {
        const payload: unknown = JSON.parse(event.data);
        if (!isRecord(payload)) return;
        daemonReportRef.current = payload.status === "healthy" ? "healthy" : "degraded";
        recordSuccessfulEvent((previous) => ({
          ...previous,
          redisConnected: payload.redis_connected === true,
          sqliteConnected: payload.sqlite_connected === true,
        }), true);
      } catch {
        // Ignore malformed events. A valid event must confirm system health.
      }
    });

    es.addEventListener("agent-health", (event: MessageEvent) => {
      try {
        const payload: unknown = JSON.parse(event.data);
        if (!isRecord(payload)) return;
        recordSuccessfulEvent((previous) => ({
          ...previous,
          agentHealth: payload as unknown as Record<string, AgentHealthEntry>,
        }));
      } catch {
        // Ignore malformed events.
      }
    });

    es.addEventListener("task-started", (event: MessageEvent) => {
      try {
        const payload: unknown = JSON.parse(event.data);
        if (!isRecord(payload) || typeof payload.task_id !== "string") return;
        recordSuccessfulEvent((previous) => ({
          ...previous,
          latestTaskEvent: {
            type: "started",
            task_id: payload.task_id as string,
            label: typeof payload.label === "string" ? payload.label : undefined,
          },
          eventSequence: previous.eventSequence + 1,
        }));
      } catch {
        // Ignore malformed events.
      }
    });

    es.addEventListener("task-completed", (event: MessageEvent) => {
      try {
        const payload: unknown = JSON.parse(event.data);
        if (!isRecord(payload) || typeof payload.task_id !== "string") return;
        recordSuccessfulEvent((previous) => ({
          ...previous,
          latestTaskEvent: {
            type: "completed",
            task_id: payload.task_id as string,
            label: typeof payload.label === "string" ? payload.label : undefined,
            status: typeof payload.status === "string" ? payload.status : undefined,
          },
          eventSequence: previous.eventSequence + 1,
        }));
      } catch {
        // Ignore malformed events.
      }
    });

    es.addEventListener("error", () => {
      connectionFailedRef.current = true;
      awaitingFreshDaemonStatusRef.current = daemonReportRef.current !== null;
      transportStateRef.current = es.readyState === EventSource.CLOSED
        ? "closed"
        : "connecting";
      updateData((previous) => applyDerivedState(previous, Date.now()));
      scheduleStateCheck();
    });
  }, [applyDerivedState, browserOnline, recordSuccessfulEvent, scheduleStateCheck, updateData]);

  useEffect(() => {
    if (!browserOnline) {
      esRef.current?.close();
      esRef.current = null;
      transportStateRef.current = "closed";
      connectionFailedRef.current = true;
      return;
    }

    const connectionTimer = window.setTimeout(connect, 0);
    return () => {
      window.clearTimeout(connectionTimer);
      esRef.current?.close();
      esRef.current = null;
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [browserOnline, connect]);

  if (!browserOnline) {
    return {
      ...data,
      connectionState: "offline",
      daemonStatus: "disconnected",
      isStale: true,
      failedDependencies: [OFFLINE_ISSUE],
      affectedFeatures: OFFLINE_ISSUE.affectedFeatures,
      reconnect: connect,
    };
  }

  return { ...data, reconnect: connect };
}
