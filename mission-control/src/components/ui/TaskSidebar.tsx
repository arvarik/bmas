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
  Search,
  Pin,
  MessagesSquare,
  AlertTriangle,
  RotateCcw,
} from "lucide-react";
import type { TaskHistoryFilters, TaskSummary } from "@/hooks/useTaskHistory";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";

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
  filters: TaskHistoryFilters;
  onFiltersChange: (filters: TaskHistoryFilters) => void;
  error: string | null;
  onRetry: () => void;
}

const PIN_STORAGE_KEY = "bmas:pinned-tasks:v1";
const PIN_EVENT = "bmas-pins-changed";

function subscribePins(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  window.addEventListener(PIN_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(PIN_EVENT, callback);
  };
}

function getPinSnapshot(): string {
  return window.localStorage.getItem(PIN_STORAGE_KEY) ?? "[]";
}

function needsAttention(task: TaskSummary): boolean {
  return task.status === "failed"
    || ["blocked", "paused", "pause_requested"].includes(task.run_state ?? "");
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

function StatusIcon({ status, runState }: { status: string; runState?: string | null }) {
  if (["blocked", "paused", "pause_requested"].includes(runState ?? "")) {
    return (
      <span
        className="task-sidebar__item-status"
        style={{ color: "var(--status-paused)" }}
        aria-label="Needs operator attention"
      >
        !
      </span>
    );
  }
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
  filters,
  onFiltersChange,
  error,
  onRetry,
}: TaskSidebarProps) {
  const pathname = usePathname();
  const [searchDraft, setSearchDraft] = useState(filters.search);
  const [dateChoice, setDateChoice] = useState("");
  const pinSnapshot = useSyncExternalStore(subscribePins, getPinSnapshot, () => "[]");
  const pinnedIds = useMemo(() => {
    try {
      const values = JSON.parse(pinSnapshot) as unknown;
      return Array.isArray(values)
        ? values.filter((value): value is string => typeof value === "string")
        : [];
    } catch {
      return [];
    }
  }, [pinSnapshot]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      if (searchDraft !== filters.search) {
        onFiltersChange({ ...filters, search: searchDraft });
      }
    }, 250);
    return () => window.clearTimeout(timeout);
  }, [filters, onFiltersChange, searchDraft]);

  const visibleTasks = useMemo(() => {
    const filtered = filters.status === "attention"
      ? tasks.filter(needsAttention)
      : tasks;
    const pins = new Set(pinnedIds);
    return [...filtered].sort((left, right) => {
      const pinOrder = Number(pins.has(right.id)) - Number(pins.has(left.id));
      if (pinOrder) return pinOrder;
      return Number(needsAttention(right)) - Number(needsAttention(left));
    });
  }, [filters.status, pinnedIds, tasks]);
  const pinnedTaskIds = new Set(pinnedIds);
  const pinnedTasks = visibleTasks.filter((task) => pinnedTaskIds.has(task.id));
  const groups = groupByDate(
    visibleTasks.filter((task) => !pinnedTaskIds.has(task.id)),
  );
  if (pinnedTasks.length) {
    const datedGroups = [...groups.entries()];
    groups.clear();
    groups.set("Pinned", pinnedTasks);
    for (const [label, tasksForDate] of datedGroups) {
      groups.set(label, tasksForDate);
    }
  }

  const updateFilter = (patch: Partial<TaskHistoryFilters>) => {
    onFiltersChange({ ...filters, ...patch });
  };

  const hasActiveFilters = Boolean(
    filters.search
    || filters.status
    || filters.dateFrom
    || filters.minCost
    || filters.maxCost,
  );
  const taskHistoryPermissionDenied = error
    ? /\b40[13]\b|unauthori[sz]ed|forbidden|permission/i.test(error)
    : false;

  const clearFilters = () => {
    setSearchDraft("");
    setDateChoice("");
    onFiltersChange({
      search: "",
      status: "",
      dateFrom: "",
      minCost: "",
      maxCost: "",
    });
  };

  const togglePin = (taskId: string) => {
    const next = pinnedIds.includes(taskId)
      ? pinnedIds.filter((id) => id !== taskId)
      : [...pinnedIds, taskId];
    window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(next));
    window.dispatchEvent(new Event(PIN_EVENT));
  };

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
          <div className="task-sidebar__filters">
            <label className="task-sidebar__search">
              <Search size={13} aria-hidden="true" />
              <span className="sr-only">Search tasks</span>
              <input
                value={searchDraft}
                onChange={(event) => setSearchDraft(event.target.value)}
                placeholder="Search tasks"
              />
            </label>
            <div className="task-sidebar__filter-grid">
              <select
                aria-label="Filter tasks by status"
                value={filters.status}
                onChange={(event) => updateFilter({ status: event.target.value })}
              >
                <option value="">All states</option>
                <option value="attention">Needs attention</option>
                <option value="pending">Queued</option>
                <option value="running">Running</option>
                <option value="failed">Failed</option>
                <option value="completed">Completed</option>
              </select>
              <select
                aria-label="Filter tasks by date"
                value={dateChoice}
                onChange={(event) => {
                  const choice = event.target.value;
                  setDateChoice(choice);
                  const days = Number(choice);
                  const threshold = new Date();
                  threshold.setHours(0, 0, 0, 0);
                  threshold.setDate(threshold.getDate() - days);
                  updateFilter({ dateFrom: choice ? threshold.toISOString() : "" });
                }}
              >
                <option value="">Any date</option>
                <option value="0">Today</option>
                <option value="7">Last 7 days</option>
                <option value="30">Last 30 days</option>
              </select>
              <select
                aria-label="Filter tasks by cost"
                value={filters.minCost}
                onChange={(event) => updateFilter({ minCost: event.target.value })}
              >
                <option value="">Any cost</option>
                <option value="0.000001">With cost</option>
                <option value="0.1">Over $0.10</option>
                <option value="1">Over $1.00</option>
              </select>
            </div>
          </div>
          {!isLoading && error && tasks.length === 0 ? (
            <div className="task-sidebar__state" role="status">
              <AlertTriangle size={17} aria-hidden="true" />
              <strong>{taskHistoryPermissionDenied ? "Task history access denied" : "Task history unavailable"}</strong>
              <span>
                {taskHistoryPermissionDenied
                  ? "Your current access cannot read tasks."
                  : "Open system status for the connection details."}
              </span>
              <div className="task-sidebar__state-actions">
                <button type="button" onClick={onRetry}>
                  <RotateCcw size={12} aria-hidden="true" /> Retry
                </button>
                <Link href="/infra">Open operations</Link>
              </div>
            </div>
          ) : null}
          {!isLoading && error && tasks.length > 0 ? (
            <div className="task-sidebar__stale" role="status">
              <AlertTriangle size={13} aria-hidden="true" />
              <span>The latest tasks are unavailable.</span>
              <button type="button" onClick={onRetry}>Retry</button>
            </div>
          ) : null}
          {isLoading && tasks.length === 0 && (
            <div className="task-sidebar__loading">
              <div className="shimmer" style={{ height: 32, borderRadius: "var(--radius-sm)" }} />
              <div className="shimmer" style={{ height: 32, borderRadius: "var(--radius-sm)", marginTop: 4 }} />
              <div className="shimmer" style={{ height: 32, borderRadius: "var(--radius-sm)", marginTop: 4 }} />
            </div>
          )}

          {!isLoading && !error && visibleTasks.length === 0 && (
            <div className="task-sidebar__empty">
              <span>{hasActiveFilters ? "No tasks match these filters." : "No tasks yet."}</span>
              {hasActiveFilters ? (
                <button type="button" onClick={clearFilters}>Clear filters</button>
              ) : null}
            </div>
          )}

          {Array.from(groups.entries()).map(([label, groupTasks]) => (
            <div key={label} className="task-sidebar__group">
              <div className="task-sidebar__group-label">{label}</div>
              {groupTasks.map((task) => {
                const isActive = activeTaskId === task.id;
                return (
                  <div
                    key={task.id}
                    className={`task-sidebar__item ${isActive ? "task-sidebar__item--active" : ""}`}
                  >
                    {isActive && <div className="task-sidebar__active-bar" />}
                    <Link href={`/task/${task.id}`} className="task-sidebar__item-link">
                      <StatusIcon status={task.status} runState={task.run_state} />
                      <div className="task-sidebar__item-text">
                        <span className="task-sidebar__item-id">
                          {task.id}
                        </span>
                        <span className="task-sidebar__item-label">
                          {task.label}
                        </span>
                      </div>
                    </Link>
                    <button
                      type="button"
                      className={`task-sidebar__pin ${pinnedIds.includes(task.id) ? "task-sidebar__pin--active" : ""}`}
                      onClick={() => togglePin(task.id)}
                      aria-label={`${pinnedIds.includes(task.id) ? "Unpin" : "Pin"} ${task.label}`}
                    >
                      <Pin size={12} fill={pinnedIds.includes(task.id) ? "currentColor" : "none"} />
                    </button>
                  </div>
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
          href="/sessions"
          className={`task-sidebar__system-item ${pathname === "/sessions" ? "task-sidebar__system-item--active" : ""}`}
          title={collapsed ? "Hermes Sessions" : undefined}
        >
          <MessagesSquare size={16} />
          {!collapsed && <span>Sessions</span>}
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
