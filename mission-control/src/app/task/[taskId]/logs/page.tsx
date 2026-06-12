"use client";

/**
 * Logs Tab — /task/[taskId]/logs
 *
 * Two rendering modes:
 * - Running (isLive): TaskLogTerminal per role with live streaming from
 *   useTaskData().logs
 * - Completed (!isLive): Styled <pre> blocks from REST fetch of archived
 *   logs (GET /api/tasks/{id}/logs)
 *
 */

import dynamic from "next/dynamic";
import { useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import { useTaskData } from "../TaskStreamContext";
import { Skeleton } from "@/components/ui/Skeleton";
import { Panel } from "@/components/ui/Panel";
import { AGENT_COLORS, NODE_LABELS, type AgentRole } from "@/lib/design-tokens";
import { Terminal as TerminalIcon } from "lucide-react";
import { AgentTrace } from "@/components/features/AgentTrace";
import { TurnInspector } from "@/components/features/TurnInspector";


const TaskLogTerminal = dynamic(() => import("@/components/features/TaskLogTerminal"), {
  ssr: false,
  loading: () => (
    <div style={{
      height: "100%", background: "var(--surface-overlay)",
      borderRadius: "var(--radius-lg)", display: "flex",
      alignItems: "center", justifyContent: "center",
    }}>
      <Skeleton variant="text" />
    </div>
  ),
});

const ROLES: { role: AgentRole; label: string }[] = [
  { role: "planner", label: "Node 1" },
  { role: "executor", label: "Node 2" },
  { role: "auditor", label: "Node 3" },
];

// ── Level formatting for archived logs ────────────────────────────────

const LEVEL_COLORS: Record<string, string> = {
  error: "var(--status-error)",
  warn: "hsl(38, 92%, 50%)",
  warning: "hsl(38, 92%, 50%)",
  info: "var(--status-running)",
  debug: "var(--text-tertiary)",
};

// ── Archived log entry type from REST ─────────────────────────────────

interface ArchivedLog {
  id: number;
  agent_role: string;
  level: string;
  message: string;
  created_at: string;
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
  const [activeTab, setActiveTab] = useState<AgentRole>("planner");

  // Trace/Raw toggle — default to trace when trace data exists
  type LogViewMode = "trace" | "raw";
  const hasTraces = traceEvents.length > 0;
  const [userLogView, setUserLogView] = useState<LogViewMode | null>(null);
  // If user hasn't explicitly picked, auto-select based on trace availability
  const logView: LogViewMode = userLogView ?? (hasTraces ? "trace" : "raw");
  const setLogView = setUserLogView;

  // TurnInspector state
  const [selectedTurnId, setSelectedTurnId] = useState<string | null>(null);

  // ── Archived logs for completed tasks ─────────────────────────────
  const [archivedLogs, setArchivedLogs] = useState<ArchivedLog[]>([]);
  const [archiveLoading, setArchiveLoading] = useState(false);

  const fetchArchived = useCallback(async () => {
    if (isLive || !taskId) return;
    setArchiveLoading(true);
    try {
      const res = await fetch(`/api/tasks/${taskId}/logs`);
      if (res.ok) {
        const data = await res.json();
        setArchivedLogs(Array.isArray(data) ? data : data.logs ?? []);
      }
    } catch {
      // Archived logs are best-effort
    } finally {
      setArchiveLoading(false);
    }
  }, [isLive, taskId]);

  useEffect(() => {
    if (!isLive) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch, setState is in the callback not the effect body
      void fetchArchived();
    }
  }, [isLive, fetchArchived]);

  // ── Running: live terminals ───────────────────────────────────────
  if (isLive) {
    return (
      <div className="view-container logs-view">
        {/* Trace/Raw segmented control */}
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)", padding: "0 0 var(--space-2) 0" }}>
          <div role="tablist" style={{ display: "inline-flex", background: "var(--surface-hover)", borderRadius: "var(--radius-sm)", padding: 2 }}>
            <button
              role="tab"
              aria-selected={logView === "trace"}
              onClick={() => setLogView("trace")}
              style={{
                padding: "2px 10px", fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)",
                borderRadius: "var(--radius-sm)", border: "none", cursor: "pointer", fontFamily: "var(--font-sans)",
                background: logView === "trace" ? "var(--surface-overlay)" : "transparent",
                color: logView === "trace" ? "var(--text-primary)" : "var(--text-tertiary)",
                transition: "background 150ms ease, color 150ms ease",
              }}
            >
              Trace
            </button>
            <button
              role="tab"
              aria-selected={logView === "raw"}
              onClick={() => setLogView("raw")}
              style={{
                padding: "2px 10px", fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)",
                borderRadius: "var(--radius-sm)", border: "none", cursor: "pointer", fontFamily: "var(--font-sans)",
                background: logView === "raw" ? "var(--surface-overlay)" : "transparent",
                color: logView === "raw" ? "var(--text-primary)" : "var(--text-tertiary)",
                transition: "background 150ms ease, color 150ms ease",
              }}
            >
              Raw
            </button>
          </div>
        </div>

        {/* Trace mode */}
        {logView === "trace" ? (
          <div style={{ flex: 1, minHeight: 200 }}>
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
          <>
            {/* Existing raw log UI */}
        {/* Mobile tab bar */}
        <div className="logs-tabs">
          {ROLES.map(({ role, label }) => {
            const isActive = activeTab === role;
            const roleLogCount = liveLogs.filter((l) => l.agent_role === role).length;
            return (
              <button
                key={role}
                className={`logs-tab ${isActive ? "logs-tab--active" : ""}`}
                onClick={() => setActiveTab(role)}
                style={{
                  borderBottomColor: isActive ? AGENT_COLORS[role] : "transparent",
                  color: isActive ? AGENT_COLORS[role] : undefined,
                }}
              >
                <span
                  className="logs-tab__dot"
                  style={{ background: AGENT_COLORS[role] }}
                />
                {label}
                {roleLogCount > 0 && (
                  <span className="logs-tab__count">{roleLogCount}</span>
                )}
              </button>
            );
          })}
        </div>

        {/* Mobile: single terminal */}
        <div className="logs-mobile-terminal">
          <TaskLogTerminal
            role={activeTab}
            logs={liveLogs.filter((l) => l.agent_role === activeTab)}
            key={activeTab}
          />
        </div>

        {/* Desktop: all three side-by-side */}
        <div className="logs-desktop-grid">
          {ROLES.map(({ role }) => (
            <div key={role} className="logs-desktop-terminal">
              <TaskLogTerminal
                role={role}
                logs={liveLogs.filter((l) => l.agent_role === role)}
              />
            </div>
          ))}
        </div>
          </>
        )}

        {/* TurnInspector slide-over */}
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

  // ── Completed: archived log viewer ────────────────────────────────

  if (archiveLoading) {
    return (
      <div className="view-container">
        <Panel title="Logs" status="loading" />
      </div>
    );
  }

  if (archivedLogs.length === 0) {
    return (
      <div className="view-container">
        <Panel
          title="Logs"
          status="empty"
          emptyIcon={TerminalIcon}
          emptyMessage="No logs recorded"
          emptyHint="This task has no recorded log output."
        />
      </div>
    );
  }

  // Group archived logs by role
  const byRole = ROLES.map(({ role, label }) => ({
    role,
    label,
    entries: archivedLogs.filter((l) => l.agent_role === role),
  }));

  return (
    <div className="view-container logs-view">
      {/* Mobile tab bar for archived view */}
      <div className="logs-tabs">
        {ROLES.map(({ role, label }) => {
          const isActive = activeTab === role;
          const count = archivedLogs.filter((l) => l.agent_role === role).length;
          return (
            <button
              key={role}
              className={`logs-tab ${isActive ? "logs-tab--active" : ""}`}
              onClick={() => setActiveTab(role)}
              style={{
                borderBottomColor: isActive ? AGENT_COLORS[role] : "transparent",
                color: isActive ? AGENT_COLORS[role] : undefined,
              }}
            >
              <span className="logs-tab__dot" style={{ background: AGENT_COLORS[role] }} />
              {label}
              {count > 0 && <span className="logs-tab__count">{count}</span>}
            </button>
          );
        })}
      </div>

      {/* Mobile: single role */}
      <div className="logs-mobile-terminal">
        <ArchivedLogPane
          role={activeTab}
          entries={archivedLogs.filter((l) => l.agent_role === activeTab)}
        />
      </div>

      {/* Desktop: all roles */}
      <div className="logs-desktop-grid">
        {byRole.map(({ role, entries }) => (
          <div key={role} className="logs-desktop-terminal">
            <ArchivedLogPane role={role} entries={entries} />
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Archived log pane (styled <pre>) ──────────────────────────────────

function ArchivedLogPane({
  role,
  entries,
}: {
  role: AgentRole;
  entries: ArchivedLog[];
}) {
  const agentColor = AGENT_COLORS[role];

  return (
    <div className="archived-log-pane" style={{ borderColor: agentColor }}>
      <div className="archived-log-pane__header" style={{ color: agentColor }}>
        <TerminalIcon size={14} />
        <span>{NODE_LABELS[role]} Agent Log</span>
        <span className="archived-log-pane__count">
          {entries.length} {entries.length === 1 ? "entry" : "entries"}
        </span>
      </div>
      {entries.length === 0 ? (
        <div className="archived-log-pane__empty">
          No output from this agent.
        </div>
      ) : (
        <pre className="archived-log-pane__content">
          {entries.map((entry) => {
            const ts = new Date(entry.created_at).toLocaleTimeString();
            const color = LEVEL_COLORS[entry.level?.toLowerCase()] ?? "var(--text-secondary)";
            return (
              <div key={entry.id} className="archived-log-pane__line">
                <span className="archived-log-pane__ts">{ts}</span>
                <span className="archived-log-pane__level" style={{ color }}>
                  {(entry.level ?? "info").toUpperCase().slice(0, 3)}
                </span>
                <span className="archived-log-pane__msg">{entry.message}</span>
              </div>
            );
          })}
        </pre>
      )}
    </div>
  );
}
