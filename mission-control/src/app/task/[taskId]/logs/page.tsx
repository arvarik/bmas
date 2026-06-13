"use client";

/**
 * Logs Tab — /task/[taskId]/logs
 *
 * Displays a unified, chronological stream of distributed agent logs
 * via DistributedLogStream. Agents are discovered dynamically from the
 * log data — no hardcoded role names. Handles any agent profile the
 * daemon spawns (expert.*, worker.*, universal-*, or legacy planner/executor).
 *
 * Two modes:
 *  - Live (isLive): streams from SSE useTaskData().logs
 *  - Completed (!isLive): fetches archived REST logs, falls back to
 *    traceEvents if archived logs are empty
 *
 */

import { useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import { useTaskData } from "../TaskStreamContext";
import { Panel } from "@/components/ui/Panel";
import { DistributedLogStream } from "@/components/features/DistributedLogStream";
import type { LogLine } from "@/components/features/DistributedLogStream";
import { AgentTrace } from "@/components/features/AgentTrace";
import { TurnInspector } from "@/components/features/TurnInspector";
import { RefreshCw, AlertTriangle, Terminal as TerminalIcon } from "lucide-react";

// ── Archived log shape from REST ──────────────────────────────────────

interface ArchivedLog {
  id: number;
  agent_role: string;
  level: string;
  message: string;
  created_at: string;
}

// ── Mappers ───────────────────────────────────────────────────────────

function liveLogToLine(log: { id: string; agent_role: string; level: string; message: string; timestamp: string }): LogLine {
  return {
    id: log.id,
    agent: log.agent_role,
    level: log.level,
    message: log.message,
    timestamp: log.timestamp,
  };
}

function archivedLogToLine(log: ArchivedLog): LogLine {
  return {
    id: String(log.id),
    agent: log.agent_role,
    level: log.level,
    message: log.message,
    timestamp: log.created_at,
  };
}

// ── Mode Toggle (hoisted to module level to satisfy react-hooks/static-components) ────

interface ModeToggleProps {
  viewMode: "stream" | "trace";
  hasTraces: boolean;
  onSetMode: (mode: "stream" | "trace") => void;
}

function ModeToggle({ viewMode, hasTraces, onSetMode }: ModeToggleProps) {
  return (
    <div className="logs-mode-toggle">
      <button
        className={`logs-mode-btn ${viewMode === "stream" ? "logs-mode-btn--active" : ""}`}
        onClick={() => onSetMode("stream")}
      >
        Stream
      </button>
      {hasTraces && (
        <button
          className={`logs-mode-btn ${viewMode === "trace" ? "logs-mode-btn--active" : ""}`}
          onClick={() => onSetMode("trace")}
        >
          Trace
        </button>
      )}
    </div>
  );
}

// ── Component ─────────────────────────────────────────────────────────

export default function LogsPage() {
  const { taskId } = useParams();
  const {
    isLive,
    logs: liveLogs,
    traceEvents,
    activeTurns,
    completedTurns,
    boardEntries,
    rejectedEntries,
  } = useTaskData();

  // TurnInspector (clickable from trace view)
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);

  // View mode: "stream" (raw logs) or "trace" (structured turns)
  const hasTraces = traceEvents.length > 0;
  const [viewMode, setViewMode] = useState<"stream" | "trace">("stream");

  // ── Archived logs state (completed tasks) ─────────────────────────
  const [archivedLogs, setArchivedLogs] = useState<ArchivedLog[]>([]);
  const [archiveLoading, setArchiveLoading] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);

  const fetchArchived = useCallback(async () => {
    if (isLive || !taskId) return;
    setArchiveLoading(true);
    setArchiveError(null);
    try {
      const res = await fetch(`/api/tasks/${taskId}/logs?limit=1000`);
      if (res.ok) {
        const data = await res.json();
        // Daemon returns { entries: [...], total: N } — NOT { logs: [...] }
        setArchivedLogs(Array.isArray(data) ? data : data.entries ?? data.logs ?? []);
      } else {
        setArchiveError(`Daemon returned ${res.status} — logs unavailable`);
      }
    } catch (err) {
      setArchiveError(err instanceof Error ? err.message : "Network error fetching logs");
    } finally {
      setArchiveLoading(false);
    }
  }, [isLive, taskId]);

  useEffect(() => {
    if (!isLive) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      void fetchArchived();
    }
  }, [isLive, fetchArchived]);

  // ── Live mode ─────────────────────────────────────────────────────
  if (isLive) {
    const liveLines: LogLine[] = liveLogs.map(liveLogToLine);

    return (
      <div className="view-container logs-view">
        <ModeToggle viewMode={viewMode} hasTraces={hasTraces} onSetMode={setViewMode} />

        {viewMode === "trace" ? (
          <div style={{ flex: 1, minHeight: 0 }}>
            <Panel title="Agent Trace" subtitle={`${traceEvents.length} events`}>
              <AgentTrace
                traceEvents={traceEvents}
                activeTurns={activeTurns}
                completedTurns={completedTurns}
                onTurnClick={setSelectedTurnId}
              />
            </Panel>
          </div>
        ) : (
          <div className="logs-stream-container">
            <DistributedLogStream
              lines={liveLines}
              isLive={true}
              sourceLabel="SSE stream"
            />
          </div>
        )}

        {selectedTurnId && (
          <TurnInspector
            turnId={selectedTurnId}
            activeTurns={activeTurns}
            completedTurns={completedTurns}
            traceEvents={traceEvents}
            boardEntries={boardEntries}
            rejectedEntries={rejectedEntries}
            onClose={() => setSelectedTurnId(null)}
          />
        )}
      </div>
    );
  }

  // ── Completed: loading ─────────────────────────────────────────────
  if (archiveLoading) {
    return (
      <div className="view-container">
        <Panel title="Logs" status="loading" />
      </div>
    );
  }

  // ── Completed: error ──────────────────────────────────────────────
  if (archiveError) {
    return (
      <div className="view-container">
        <div className="logs-error-state">
          <AlertTriangle size={16} style={{ flexShrink: 0 }} />
          <span>{archiveError}</span>
          <button className="logs-retry-btn" onClick={() => void fetchArchived()}>
            <RefreshCw size={12} />
            Retry
          </button>
        </div>

        {/* Still offer trace view as fallback */}
        {traceEvents.length > 0 && (
          <div style={{ flex: 1, minHeight: 0, marginTop: "var(--space-3)" }}>
            <Panel title="Agent Trace (fallback)" subtitle={`${traceEvents.length} events`}>
              <AgentTrace
                traceEvents={traceEvents}
                activeTurns={activeTurns}
                completedTurns={completedTurns}
                onTurnClick={setSelectedTurnId}
              />
            </Panel>
          </div>
        )}
      </div>
    );
  }

  // ── Completed: no logs but has trace ──────────────────────────────
  if (archivedLogs.length === 0 && traceEvents.length > 0) {
    return (
      <div className="view-container logs-view">
        <div className="logs-fallback-notice">
          No raw logs recorded — showing agent trace instead.
        </div>
        <div style={{ flex: 1, minHeight: 0 }}>
          <Panel title="Agent Trace" subtitle={`${traceEvents.length} events`}>
            <AgentTrace
              traceEvents={traceEvents}
              activeTurns={activeTurns}
              completedTurns={completedTurns}
              onTurnClick={setSelectedTurnId}
            />
          </Panel>
        </div>
        {selectedTurnId && (
          <TurnInspector
            turnId={selectedTurnId}
            activeTurns={activeTurns}
            completedTurns={completedTurns}
            traceEvents={traceEvents}
            boardEntries={boardEntries}
            rejectedEntries={rejectedEntries}
            onClose={() => setSelectedTurnId(null)}
          />
        )}
      </div>
    );
  }

  // ── Completed: truly empty ────────────────────────────────────────
  if (archivedLogs.length === 0) {
    return (
      <div className="view-container">
        <Panel
          title="Logs"
          status="empty"
          emptyIcon={TerminalIcon}
          emptyMessage="No logs recorded"
          emptyHint="This task produced no log output."
        />
      </div>
    );
  }

  // ── Completed: archived log stream ────────────────────────────────
  const archivedLines: LogLine[] = archivedLogs.map(archivedLogToLine);

  return (
    <div className="view-container logs-view">
      <ModeToggle />

      {viewMode === "trace" && traceEvents.length > 0 ? (
        <div style={{ flex: 1, minHeight: 0 }}>
          <Panel title="Agent Trace" subtitle={`${traceEvents.length} events`}>
            <AgentTrace
              traceEvents={traceEvents}
              activeTurns={activeTurns}
              completedTurns={completedTurns}
              onTurnClick={setSelectedTurnId}
            />
          </Panel>
        </div>
      ) : (
        <div className="logs-stream-container">
          <DistributedLogStream
            lines={archivedLines}
            isLive={false}
            sourceLabel={`${archivedLogs.length} archived`}
          />
        </div>
      )}

      {selectedTurnId && (
        <TurnInspector
          turnId={selectedTurnId}
          activeTurns={activeTurns}
          completedTurns={completedTurns}
          traceEvents={traceEvents}
          boardEntries={boardEntries}
          rejectedEntries={rejectedEntries}
          onClose={() => setSelectedTurnId(null)}
        />
      )}
    </div>
  );
}
