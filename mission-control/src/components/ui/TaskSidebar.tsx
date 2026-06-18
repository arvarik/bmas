"use client";

/**
 * TaskSidebar — task-history sidebar (replaces feature-nav Sidebar).
 *
 * Shows: [+ New Task] CTA, date-grouped task history with status
 * indicators, system nav links (Infra, Skills), and agent health dots.
 *
 * Uses Next.js <Link> for all navigation. Active state is derived
 * from usePathname() — no imperative callbacks needed.
 *
 */


import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  Plus,
  Server,
  Bot,
  Settings,
  PanelLeftClose,
  PanelLeftOpen,
  ChevronDown,
} from "lucide-react";
import type { TaskSummary } from "@/hooks/useTaskHistory";

// ── Props ─────────────────────────────────────────────────────────────

export interface TaskSidebarProps {
  tasks: TaskSummary[];
  agentHealth: Record<string, { alive: boolean }>;
  collapsed: boolean;
  onToggleCollapse: () => void;
  mobileOpen: boolean;
  isLoading: boolean;
  hasMore: boolean;
  onLoadMore: () => void;
}

// ── Date grouping ─────────────────────────────────────────────────────

function groupByDate(tasks: TaskSummary[]): Map<string, TaskSummary[]> {
  const groups = new Map<string, TaskSummary[]>();

  // Normalize "today" to midnight local time for calendar-day grouping
  const today = new Date();
  today.setHours(0, 0, 0, 0);

  for (const task of tasks) {
    const created = new Date(task.created_at);
    const createdDay = new Date(created);
    createdDay.setHours(0, 0, 0, 0);

    const diffDays = Math.floor(
      (today.getTime() - createdDay.getTime()) / 86400000
    );
    let label: string;
    if (diffDays === 0) label = "Today";
    else if (diffDays === 1) label = "Yesterday";
    else if (diffDays <= 7) label = "Last 7 Days";
    else if (diffDays <= 30) label = "Last 30 Days";
    else
      label = created.toLocaleDateString(undefined, {
        month: "long",
        year: "numeric",
      });

    if (!groups.has(label)) groups.set(label, []);
    groups.get(label)!.push(task);
  }
  return groups;
}

// ── Status indicator ──────────────────────────────────────────────────

function StatusIcon({ status }: { status: string }) {
  switch (status) {
    case "running":
      return (
        <span
          className="task-sidebar__item-status pulse-dot"
          style={{ background: "var(--status-running)" }}
          aria-label="Running"
        />
      );
    case "completed":
      return (
        <span
          className="task-sidebar__item-status"
          style={{ color: "var(--status-success)" }}
          aria-label="Completed"
        >
          ✓
        </span>
      );
    case "failed":
      return (
        <span
          className="task-sidebar__item-status"
          style={{ color: "var(--status-error)" }}
          aria-label="Failed"
        >
          ✗
        </span>
      );
    default:
      return (
        <span
          className="task-sidebar__item-status"
          style={{ color: "var(--status-pending)" }}
          aria-label="Pending"
        >
          ○
        </span>
      );
  }
}

// ── Agent health dots (reused from current Sidebar) ───────────────────

const AGENT_DOTS = [
  { role: "planner", label: "Node 1" },
  { role: "executor", label: "Node 2" },
  { role: "auditor", label: "Node 3" },
] as const;

// ── Component ─────────────────────────────────────────────────────────

export function TaskSidebar({
  tasks,
  agentHealth,
  collapsed,
  onToggleCollapse,
  mobileOpen,
  isLoading,
  hasMore,
  onLoadMore,
}: TaskSidebarProps) {
  const pathname = usePathname();
  const groups = groupByDate(tasks);

  // Determine active task ID from pathname
  const activeTaskId =
    pathname.startsWith("/task/")
      ? pathname.split("/")[2] ?? null
      : null;

  const sidebarClass = [
    "sidebar task-sidebar",
    collapsed ? "sidebar--collapsed" : "sidebar--expanded",
    mobileOpen ? "sidebar--mobile-open" : "",
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <aside className={sidebarClass}>
      {/* ── Collapse Toggle ───────────────────────────────────── */}
      <div className="sidebar__toggle-row">
        <button
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="sidebar__toggle-btn"
        >
          {collapsed ? (
            <PanelLeftOpen size={16} />
          ) : (
            <PanelLeftClose size={16} />
          )}
        </button>
      </div>

      {/* ── New Task CTA ──────────────────────────────────────── */}
      <div className="task-sidebar__cta">
        <Link
          href="/"
          className="task-sidebar__new-btn"
          title="New Task"
        >
          <Plus size={16} />
          {!collapsed && <span>New Task</span>}
        </Link>
      </div>

      {/* ── Task List (scrollable, hidden when collapsed) ─────── */}
      {!collapsed && (
        <div className="task-sidebar__scroll">
          {isLoading && tasks.length === 0 && (
            <div className="task-sidebar__loading">
              <div className="shimmer" style={{ height: 32, borderRadius: "var(--radius-sm)" }} />
              <div className="shimmer" style={{ height: 32, borderRadius: "var(--radius-sm)", marginTop: 4 }} />
              <div className="shimmer" style={{ height: 32, borderRadius: "var(--radius-sm)", marginTop: 4 }} />
            </div>
          )}

          {!isLoading && tasks.length === 0 && (
            <div className="task-sidebar__empty">
              No tasks yet
            </div>
          )}

          {Array.from(groups.entries()).map(([label, groupTasks]) => (
            <div key={label} className="task-sidebar__group">
              <div className="task-sidebar__group-label">{label}</div>
              {groupTasks.map((task) => {
                const isActive = activeTaskId === task.id;
                return (
                  <Link
                    key={task.id}
                    href={`/task/${task.id}`}
                    className={`task-sidebar__item ${isActive ? "task-sidebar__item--active" : ""}`}
                  >
                    {isActive && <div className="task-sidebar__active-bar" />}
                    <StatusIcon status={task.status} />
                    <div className="task-sidebar__item-text">
                      <span className="task-sidebar__item-id">
                        {task.id}
                      </span>
                      <span className="task-sidebar__item-label">
                        {task.label}
                      </span>
                    </div>
                  </Link>
                );
              })}
            </div>
          ))}

          {/* Load more button */}
          {hasMore && (
            <button
              className="task-sidebar__load-more"
              onClick={onLoadMore}
            >
              <ChevronDown size={14} />
              Load more
            </button>
          )}
        </div>
      )}

      {/* ── System Section ────────────────────────────────────── */}
      <div className="task-sidebar__system-section">
        {!collapsed && (
          <div className="task-sidebar__system-divider" />
        )}

        <Link
          href="/agents"
          className={`task-sidebar__system-item ${pathname === "/agents" ? "task-sidebar__system-item--active" : ""}`}
          title={collapsed ? "Agents" : undefined}
        >
          <Bot size={16} />
          {!collapsed && <span>Agents</span>}
        </Link>

        <Link
          href="/infra"
          className={`task-sidebar__system-item ${pathname === "/infra" ? "task-sidebar__system-item--active" : ""}`}
          title={collapsed ? "Infrastructure" : undefined}
        >
          <Server size={16} />
          {!collapsed && <span>Infrastructure</span>}
        </Link>

        <Link
          href="/settings"
          className={`task-sidebar__system-item ${pathname === "/settings" ? "task-sidebar__system-item--active" : ""}`}
          title={collapsed ? "Settings" : undefined}
        >
          <Settings size={16} />
          {!collapsed && <span>Settings</span>}
        </Link>
      </div>

      {/* ── Agent Health Dots ──────────────────────────────────── */}
      <div className="sidebar__footer">
        {AGENT_DOTS.map((agent) => {
          const isHealthy = agentHealth[agent.role]?.alive ?? false;
          return (
            <div
              key={agent.role}
              title={`${agent.label}: ${isHealthy ? "Connected" : "Disconnected"}`}
              className="sidebar__agent-dot"
              style={{
                background: isHealthy
                  ? "var(--status-success)"
                  : "var(--status-error)",
              }}
            />
          );
        })}
      </div>
    </aside>
  );
}

export default TaskSidebar;
