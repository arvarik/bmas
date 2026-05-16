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
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";
import { Terminal as TerminalIcon } from "lucide-react";


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
  { role: "planner", label: "Planner" },
  { role: "executor", label: "Executor" },
  { role: "auditor", label: "Auditor" },
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
  const { isLive, logs: liveLogs } = useTaskData();
  const [activeTab, setActiveTab] = useState<AgentRole>("planner");

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
      void fetchArchived();
    }
  }, [isLive, fetchArchived]);

  // ── Running: live terminals ───────────────────────────────────────
  if (isLive) {
    return (
      <div className="view-container logs-view">
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
        <span>{role.charAt(0).toUpperCase() + role.slice(1)} Agent Log</span>
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
