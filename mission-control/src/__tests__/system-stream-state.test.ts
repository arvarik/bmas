import { describe, expect, it } from "vitest";
import {
  deriveSystemState,
  nextSystemStateDeadline,
  SYSTEM_EVENT_STALE_AFTER_MS,
  SYSTEM_STATUS_WAIT_MS,
} from "@/hooks/useSystemStream";

const NOW = 1_000_000;

function stateInput(overrides: Partial<Parameters<typeof deriveSystemState>[0]> = {}) {
  return {
    browserOnline: true,
    transportState: "open" as const,
    connectionFailed: false,
    awaitingFreshDaemonStatus: false,
    daemonReport: "healthy" as const,
    redisConnected: true,
    sqliteConnected: true,
    agentHealth: {},
    lastSuccessfulEventAtMs: NOW,
    lastDaemonStatusAtMs: NOW,
    transportOpenedAtMs: NOW - 1_000,
    now: NOW,
    ...overrides,
  };
}

describe("deriveSystemState", () => {
  it("requires a daemon report before it marks the system ready", () => {
    const result = deriveSystemState(stateInput({ daemonReport: null }));
    expect(result.connectionState).toBe("connecting");
  });

  it("marks a pending initial connection disconnected after the deadline", () => {
    const result = deriveSystemState(stateInput({
      transportState: "connecting",
      daemonReport: null,
      lastSuccessfulEventAtMs: null,
      lastDaemonStatusAtMs: null,
      transportOpenedAtMs: NOW - 10_000,
    }));
    expect(result.connectionState).toBe("disconnected");
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("daemon");
  });

  it("marks an open stream degraded when a reconnect lacks a fresh report", () => {
    const result = deriveSystemState(stateInput({
      awaitingFreshDaemonStatus: true,
      transportOpenedAtMs: NOW - SYSTEM_STATUS_WAIT_MS,
    }));
    expect(result.connectionState).toBe("degraded");
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("event-stream");
  });

  it("marks a silent reconnect disconnected after the deadline", () => {
    const result = deriveSystemState(stateInput({
      transportState: "connecting",
      awaitingFreshDaemonStatus: true,
      transportOpenedAtMs: NOW - SYSTEM_STATUS_WAIT_MS,
    }));
    expect(result.connectionState).toBe("disconnected");
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("daemon");
  });

  it("marks a recent healthy report ready", () => {
    const result = deriveSystemState(stateInput());
    expect(result.connectionState).toBe("ready");
    expect(result.failedDependencies).toEqual([]);
  });

  it("marks a failed Redis dependency degraded", () => {
    const result = deriveSystemState(stateInput({
      daemonReport: "degraded",
      redisConnected: false,
    }));
    expect(result.connectionState).toBe("degraded");
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("redis");
    expect(result.affectedFeatures).toContain("Task controls");
  });

  it("marks an unavailable agent degraded", () => {
    const result = deriveSystemState(stateInput({
      agentHealth: {
        planner: { alive: false, last_heartbeat: "" },
      },
    }));
    expect(result.connectionState).toBe("degraded");
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("agents");
  });

  it("marks a temporary stream failure reconnecting", () => {
    const result = deriveSystemState(stateInput({
      transportState: "connecting",
      connectionFailed: true,
    }));
    expect(result.connectionState).toBe("reconnecting");
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("event-stream");
  });

  it("keeps agent failures visible while the stream reconnects", () => {
    const result = deriveSystemState(stateInput({
      transportState: "connecting",
      connectionFailed: true,
      agentHealth: { planner: { alive: false, last_heartbeat: "" } },
    }));
    expect(result.failedDependencies.map((issue) => issue.id)).toEqual([
      "event-stream",
      "agents",
    ]);
  });

  it("marks a stale stream degraded while the transport remains open", () => {
    const result = deriveSystemState(stateInput({
      lastSuccessfulEventAtMs: NOW - SYSTEM_EVENT_STALE_AFTER_MS,
      lastDaemonStatusAtMs: NOW - SYSTEM_EVENT_STALE_AFTER_MS,
    }));
    expect(result.connectionState).toBe("degraded");
    expect(result.isStale).toBe(true);
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("event-stream");
  });

  it("does not let task activity refresh a stale daemon report", () => {
    const result = deriveSystemState(stateInput({
      lastSuccessfulEventAtMs: NOW,
      lastDaemonStatusAtMs: NOW - SYSTEM_EVENT_STALE_AFTER_MS,
    }));
    expect(result.connectionState).toBe("degraded");
    expect(result.isStale).toBe(true);
  });

  it("marks a stale failed stream disconnected", () => {
    const result = deriveSystemState(stateInput({
      transportState: "connecting",
      connectionFailed: true,
      lastSuccessfulEventAtMs: NOW - SYSTEM_EVENT_STALE_AFTER_MS,
      lastDaemonStatusAtMs: NOW - SYSTEM_EVENT_STALE_AFTER_MS,
    }));
    expect(result.connectionState).toBe("disconnected");
  });

  it("explains a closed transport after recent activity", () => {
    const result = deriveSystemState(stateInput({ transportState: "closed" }));
    expect(result.connectionState).toBe("disconnected");
    expect(result.failedDependencies.map((issue) => issue.id)).toContain("daemon");
  });

  it("uses the offline state when the browser reports no network", () => {
    const result = deriveSystemState(stateInput({ browserOnline: false }));
    expect(result.connectionState).toBe("offline");
    expect(result.failedDependencies[0]?.id).toBe("browser-network");
  });
});

describe("nextSystemStateDeadline", () => {
  it("schedules the initial daemon status deadline", () => {
    expect(nextSystemStateDeadline({
      daemonReport: null,
      awaitingFreshDaemonStatus: false,
      lastDaemonStatusAtMs: null,
      transportOpenedAtMs: NOW,
      now: NOW,
    })).toBe(NOW + SYSTEM_STATUS_WAIT_MS);
  });

  it("schedules a fresh status deadline after a reconnect", () => {
    expect(nextSystemStateDeadline({
      daemonReport: "healthy",
      awaitingFreshDaemonStatus: true,
      lastDaemonStatusAtMs: NOW,
      transportOpenedAtMs: NOW,
      now: NOW,
    })).toBe(NOW + SYSTEM_STATUS_WAIT_MS);
  });

  it("schedules the stale deadline after the fresh status deadline expires", () => {
    expect(nextSystemStateDeadline({
      daemonReport: "healthy",
      awaitingFreshDaemonStatus: true,
      lastDaemonStatusAtMs: NOW,
      transportOpenedAtMs: NOW,
      now: NOW + SYSTEM_STATUS_WAIT_MS + 1,
    })).toBe(NOW + SYSTEM_EVENT_STALE_AFTER_MS);
  });
});
