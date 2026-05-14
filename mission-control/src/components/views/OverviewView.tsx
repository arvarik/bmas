"use client";

import { useState, useCallback } from "react";
import { useBlackboard, selectTasks, type Task } from "@/hooks/useBlackboard";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { MetricCard } from "@/components/ui/MetricCard";
import { ActionButton } from "@/components/ui/ActionButton";
import { useToast } from "@/hooks/useToast";
import {
  Activity,
  Cpu,
  DollarSign,
  Clock,
  Send,
} from "lucide-react";
import type { StatusType } from "@/lib/design-tokens";
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";

// ── Compact Task Input ────────────────────────────────────────────────

function QuickTaskInput({ onNavigateToOperator }: { onNavigateToOperator: () => void }) {
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const { toast } = useToast();

  const handleSubmit = useCallback(async () => {
    if (!task.trim() || submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: task.trim() }),
      });
      if (res.ok) {
        const data = (await res.json()) as { task_id?: string };
        toast({ type: "success", message: `Task ${data.task_id ?? ""} submitted.` });
        setTask("");
      } else {
        const body = (await res.json().catch(() => ({}))) as { error?: string };
        toast({ type: "error", message: body.error ?? `HTTP ${res.status}` });
      }
    } catch (err) {
      toast({ type: "error", message: err instanceof Error ? err.message : "Network error" });
    } finally {
      setSubmitting(false);
    }
  }, [task, submitting, toast]);

  return (
    <div className="overview-task-input">
      <Send size={16} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
      <input
        type="text"
        value={task}
        onChange={(e) => setTask(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void handleSubmit(); } }}
        placeholder="Quick task submission…"
        disabled={submitting}
      />
      <ActionButton
        variant="primary"
        loading={submitting}
        disabled={!task.trim()}
        onClick={handleSubmit}
        style={{ height: 32, padding: "0 12px", fontSize: "var(--text-xs)" }}
      >
        Submit
      </ActionButton>
      <button
        className="overview-expand-btn"
        onClick={onNavigateToOperator}
        title="Open Operator view for full controls"
      >
        Expand
      </button>
    </div>
  );
}

// ── Status Summary Cards ──────────────────────────────────────────────

function StatusCard({
  label,
  value,
  icon: Icon,
  status,
}: {
  label: string;
  value: string;
  icon: React.ComponentType<{ size?: number; style?: React.CSSProperties }>;
  status?: StatusType;
}) {
  return (
    <div className="overview-status-card">
      <div className="overview-status-card__icon">
        <Icon size={18} style={{ color: status ? `var(--status-${status})` : "var(--accent-primary)" }} />
      </div>
      <div className="overview-status-card__content">
        <span className="overview-status-card__label">{label}</span>
        <span className="overview-status-card__value">{value}</span>
      </div>
      {status && <StatusBadge status={status} />}
    </div>
  );
}

// ── Agent Health Tile ─────────────────────────────────────────────────

function AgentTile({
  name,
  role,
  alive,
  currentTask,
  lastHeartbeat,
}: {
  name: string;
  role: AgentRole;
  alive: boolean;
  currentTask?: string;
  lastHeartbeat?: string;
}) {
  const color = AGENT_COLORS[role];
  const timeStr = lastHeartbeat
    ? new Date(lastHeartbeat).toLocaleTimeString()
    : "—";

  return (
    <div className="overview-agent-tile">
      <div className="overview-agent-tile__header">
        <span
          className="overview-agent-tile__dot"
          style={{ background: alive ? "var(--status-success)" : "var(--status-error)" }}
        />
        <span className="overview-agent-tile__name" style={{ color }}>{name}</span>
        <span className="overview-agent-tile__status">
          {alive ? "Online" : "Offline"}
        </span>
      </div>
      <div className="overview-agent-tile__body">
        <span className="overview-agent-tile__task">
          {currentTask ?? "Idle"}
        </span>
        <span className="overview-agent-tile__heartbeat">{timeStr}</span>
      </div>
    </div>
  );
}

// ── Recent Activity ───────────────────────────────────────────────────

const TASK_STATUS_MAP: Record<string, StatusType> = {
  pending: "pending",
  running: "running",
  completed: "success",
  failed: "error",
};

function RecentActivity({ tasks }: { tasks: Task[] }) {
  const sorted = [...tasks].sort(
    (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
  );
  const recent = sorted.slice(0, 8);

  if (recent.length === 0) {
    return (
      <div className="overview-section">
        <h3 className="overview-section__title">Recent Activity</h3>
        <div className="overview-empty-activity">
          <Activity size={20} style={{ color: "var(--text-tertiary)" }} />
          <span>No tasks yet. Submit a task to get started.</span>
        </div>
      </div>
    );
  }

  return (
    <div className="overview-section">
      <h3 className="overview-section__title">Recent Activity</h3>
      <div className="overview-activity-list">
        {recent.map((t) => (
          <div key={t.id} className="overview-activity-item">
            <StatusBadge status={TASK_STATUS_MAP[t.status] ?? "pending"} />
            <span className="overview-activity-item__label">{t.label}</span>
            <span className="overview-activity-item__time">
              {formatRelativeTime(t.updated_at)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return new Date(iso).toLocaleDateString();
}

// ── Main View ─────────────────────────────────────────────────────────

export default function OverviewView({
  onNavigate,
}: {
  onNavigate: (id: string) => void;
}) {
  const daemonState = useBlackboard((s) => s.state);
  const daemonError = useBlackboard((s) => s.error);
  const tasks = useBlackboard(selectTasks);

  const daemonStatus: StatusType = daemonError
    ? "error"
    : daemonState
      ? "running"
      : "pending";

  const phase = daemonState?.phase ?? "Idle";
  const iteration = daemonState?.iteration ?? 0;
  const taskCount = tasks.length;
  const runningCount = tasks.filter((t) => t.status === "running").length;

  return (
    <div className="view-container overview-view">
      {/* Quick task input */}
      <QuickTaskInput onNavigateToOperator={() => onNavigate("operator")} />

      {/* Status summary row */}
      <div className="overview-status-grid">
        <StatusCard
          label="Daemon"
          value={daemonStatus === "running" ? "Connected" : daemonStatus === "error" ? "Error" : "Connecting…"}
          icon={Cpu}
          status={daemonStatus}
        />
        <StatusCard
          label="Phase"
          value={`${phase} — Iter ${iteration}`}
          icon={Activity}
        />
        <StatusCard
          label="Tasks"
          value={runningCount > 0 ? `${runningCount} running / ${taskCount} total` : `${taskCount} total`}
          icon={Clock}
        />
        <StatusCard
          label="Cost"
          value="$0.0000"
          icon={DollarSign}
        />
      </div>

      {/* Agent health */}
      <div className="overview-section">
        <h3 className="overview-section__title">Agent Health</h3>
        <div className="overview-agents-grid">
          {(["planner", "executor", "auditor"] as AgentRole[]).map((role) => {
            const agent = daemonState?.agents?.[role];
            return (
              <AgentTile
                key={role}
                name={role.charAt(0).toUpperCase() + role.slice(1)}
                role={role}
                alive={agent?.alive ?? false}
                currentTask={agent?.current_task ?? undefined}
                lastHeartbeat={agent?.last_heartbeat}
              />
            );
          })}
        </div>
      </div>

      {/* Recent activity */}
      <RecentActivity tasks={tasks} />
    </div>
  );
}
