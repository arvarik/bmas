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
  BookmarkPlus,
  Download,
  Trash2,
  X,
} from "lucide-react";
import type { TaskHistoryFilters, TaskSummary } from "@/hooks/useTaskHistory";
import { useMemo, useState, useSyncExternalStore } from "react";
import type { Ref } from "react";
import {
  parseSavedTaskViews,
  prepareTaskHistory,
  taskHistoryCsv,
  type SavedTaskView,
  type TaskHistorySort,
} from "@/lib/task-history-presentation";

// ── Props ─────────────────────────────────────────────────────────────

export interface TaskSidebarProps {
  tasks: TaskSummary[];
  agentHealth: Record<string, { alive: boolean }>;
  collapsed: boolean;
  onToggleCollapse: () => void;
  mobileOpen: boolean;
  drawerMode: boolean;
  sidebarRef?: Ref<HTMLElement>;
  onRequestClose: () => void;
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
const VIEW_STORAGE_KEY = "bmas:task-views:v1";
const VIEW_EVENT = "bmas-task-views-changed";

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

function subscribeSavedViews(callback: () => void): () => void {
  window.addEventListener("storage", callback);
  window.addEventListener(VIEW_EVENT, callback);
  return () => {
    window.removeEventListener("storage", callback);
    window.removeEventListener(VIEW_EVENT, callback);
  };
}

function getSavedViewSnapshot(): string {
  return window.localStorage.getItem(VIEW_STORAGE_KEY) ?? "[]";
}

function taskStatusLabel(status: string, runState?: string | null): string {
  if (["blocked", "paused", "pause_requested"].includes(runState ?? "")) {
    return "Needs attention";
  }
  if (status === "pending") return "Queued";
  return status.charAt(0).toUpperCase() + status.slice(1);
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
        aria-hidden="true"
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
          aria-hidden="true"
        />
      );
    case "completed":
      return (
        <span
          className="task-sidebar__item-status"
          style={{ color: "var(--status-success)" }}
          aria-hidden="true"
        >
          ✓
        </span>
      );
    case "failed":
      return (
        <span
          className="task-sidebar__item-status"
          style={{ color: "var(--status-error)" }}
          aria-hidden="true"
        >
          ✗
        </span>
      );
    default:
      return (
        <span
          className="task-sidebar__item-status"
          style={{ color: "var(--status-pending)" }}
          aria-hidden="true"
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
  drawerMode,
  sidebarRef,
  onRequestClose,
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
  const [dateChoice, setDateChoice] = useState<SavedTaskView["datePreset"]>("");
  const [sort, setSort] = useState<TaskHistorySort>("newest");
  const [selectedViewId, setSelectedViewId] = useState("");
  const [viewName, setViewName] = useState("");
  const [showSaveView, setShowSaveView] = useState(false);
  const pinSnapshot = useSyncExternalStore(subscribePins, getPinSnapshot, () => "[]");
  const savedViewSnapshot = useSyncExternalStore(
    subscribeSavedViews,
    getSavedViewSnapshot,
    () => "[]",
  );
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
  const savedViews = useMemo(
    () => parseSavedTaskViews(savedViewSnapshot),
    [savedViewSnapshot],
  );

  const clientFilters = useMemo(
    () => ({ ...filters, search: searchDraft }),
    [filters, searchDraft],
  );

  const visibleTasks = useMemo(() => {
    const filtered = prepareTaskHistory(tasks, clientFilters, sort);
    const pins = new Set(pinnedIds);
    return [...filtered].sort((left, right) => {
      const pinOrder = Number(pins.has(right.id)) - Number(pins.has(left.id));
      if (pinOrder) return pinOrder;
      return 0;
    });
  }, [clientFilters, pinnedIds, sort, tasks]);
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
    searchDraft
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
    setSelectedViewId("");
    setSort("newest");
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

  const writeSavedViews = (views: readonly SavedTaskView[]) => {
    window.localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify(views));
    window.dispatchEvent(new Event(VIEW_EVENT));
  };

  const saveCurrentView = () => {
    const name = viewName.trim();
    if (!name) return;
    const view: SavedTaskView = {
      id: `view-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      name,
      filters: { ...filters, search: searchDraft },
      sort,
      datePreset: dateChoice,
    };
    writeSavedViews([...savedViews, view]);
    setSelectedViewId(view.id);
    setViewName("");
    setShowSaveView(false);
  };

  const applySavedView = (viewId: string) => {
    setSelectedViewId(viewId);
    const view = savedViews.find((candidate) => candidate.id === viewId);
    if (!view) return;
    setSearchDraft(view.filters.search);
    setDateChoice(view.datePreset);
    setSort(view.sort);
    const threshold = new Date();
    threshold.setHours(0, 0, 0, 0);
    threshold.setDate(threshold.getDate() - Number(view.datePreset));
    onFiltersChange({
      ...view.filters,
      search: "",
      dateFrom: view.datePreset ? threshold.toISOString() : "",
    });
  };

  const deleteSelectedView = () => {
    if (!selectedViewId) return;
    writeSavedViews(savedViews.filter((view) => view.id !== selectedViewId));
    setSelectedViewId("");
  };

  const exportTasks = () => {
    const blob = new Blob([taskHistoryCsv(visibleTasks)], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `bmas-tasks-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
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
    <aside
      ref={sidebarRef}
      id="primary-navigation"
      className={sidebarClass}
      aria-label="Task and system navigation"
      aria-hidden={drawerMode && !mobileOpen ? true : undefined}
      aria-modal={drawerMode && mobileOpen ? true : undefined}
      inert={drawerMode && !mobileOpen}
      role={drawerMode ? "dialog" : undefined}
      tabIndex={drawerMode && mobileOpen ? -1 : undefined}
    >
      {drawerMode && mobileOpen ? (
        <div className="task-sidebar__drawer-close-row">
          <button
            type="button"
            className="task-sidebar__drawer-close"
            onClick={onRequestClose}
          >
            <X size={16} aria-hidden="true" />
            Close navigation
          </button>
        </div>
      ) : null}
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
              <span className="task-sidebar__control-label">Search loaded tasks</span>
              <span className="task-sidebar__search-field">
                <Search size={13} aria-hidden="true" />
                <input
                  value={searchDraft}
                  onChange={(event) => {
                    setSelectedViewId("");
                    setSearchDraft(event.target.value);
                    if (filters.search) {
                      onFiltersChange({ ...filters, search: "" });
                    }
                  }}
                  placeholder="ID, prompt, model, error"
                />
              </span>
            </label>
            <div className="task-sidebar__filter-grid">
              <label>
                <span className="task-sidebar__control-label">State</span>
                <select
                  value={filters.status}
                  onChange={(event) => {
                    setSelectedViewId("");
                    updateFilter({ status: event.target.value });
                  }}
                >
                  <option value="">All states</option>
                  <option value="attention">Needs attention</option>
                  <option value="pending">Queued</option>
                  <option value="running">Running</option>
                  <option value="failed">Failed</option>
                  <option value="completed">Completed</option>
                </select>
              </label>
              <label>
                <span className="task-sidebar__control-label">Date</span>
                <select
                  value={dateChoice}
                  onChange={(event) => {
                    setSelectedViewId("");
                    const choice = event.target.value as SavedTaskView["datePreset"];
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
              </label>
              <label>
                <span className="task-sidebar__control-label">Cost</span>
                <select
                  value={filters.minCost}
                  onChange={(event) => {
                    setSelectedViewId("");
                    updateFilter({ minCost: event.target.value });
                  }}
                >
                  <option value="">Any cost</option>
                  <option value="0.000001">With cost</option>
                  <option value="0.1">Over $0.10</option>
                  <option value="1">Over $1.00</option>
                </select>
              </label>
              <label>
                <span className="task-sidebar__control-label">Sort</span>
                <select
                  value={sort}
                  onChange={(event) => {
                    setSelectedViewId("");
                    setSort(event.target.value as TaskHistorySort);
                  }}
                >
                  <option value="newest">Newest</option>
                  <option value="oldest">Oldest</option>
                  <option value="cost-high">Highest cost</option>
                  <option value="cost-low">Lowest cost</option>
                  <option value="duration-high">Longest duration</option>
                </select>
              </label>
            </div>
            <div className="task-sidebar__saved-controls">
              <label>
                <span className="task-sidebar__control-label">Saved view</span>
                <select value={selectedViewId} onChange={(event) => applySavedView(event.target.value)}>
                  <option value="">Current filters</option>
                  {savedViews.map((view) => (
                    <option key={view.id} value={view.id}>{view.name}</option>
                  ))}
                </select>
              </label>
              <div className="task-sidebar__filter-actions">
                <button type="button" onClick={() => setShowSaveView(true)}>
                  <BookmarkPlus size={13} aria-hidden="true" /> Save
                </button>
                <button
                  type="button"
                  onClick={deleteSelectedView}
                  disabled={!selectedViewId}
                  aria-label="Delete selected saved view"
                >
                  <Trash2 size={13} aria-hidden="true" /> Delete
                </button>
                <button
                  type="button"
                  onClick={exportTasks}
                  disabled={visibleTasks.length === 0}
                  title="Export the filtered loaded tasks as CSV"
                >
                  <Download size={13} aria-hidden="true" /> Export
                </button>
              </div>
            </div>
            {showSaveView ? (
              <form
                className="task-sidebar__save-view"
                onSubmit={(event) => {
                  event.preventDefault();
                  saveCurrentView();
                }}
              >
                <label htmlFor="task-view-name">View name</label>
                <input
                  id="task-view-name"
                  value={viewName}
                  onChange={(event) => setViewName(event.target.value)}
                  autoFocus
                  maxLength={40}
                />
                <button type="submit" disabled={!viewName.trim()}>Save view</button>
                <button type="button" onClick={() => setShowSaveView(false)}>Cancel</button>
              </form>
            ) : null}
            <p className="task-sidebar__result-count" role="status">
              Showing {visibleTasks.length} of {tasks.length} loaded tasks
            </p>
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
                        <span className="task-sidebar__item-state">
                          {taskStatusLabel(task.status, task.run_state)}
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
              type="button"
              className="task-sidebar__load-more"
              onClick={onLoadMore}
              disabled={isLoading}
            >
              <ChevronDown size={14} aria-hidden="true" />
              {isLoading ? "Loading tasks" : "Load more"}
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
          const statusLabel = `${agent.label}: ${isHealthy ? "Connected" : "Disconnected"}`;
          return (
            <div
              key={agent.role}
              title={statusLabel}
              className="sidebar__agent-status"
            >
              <span
                className="sidebar__agent-dot"
                aria-hidden="true"
                style={{
                  background: isHealthy
                    ? "var(--status-success)"
                    : "var(--status-error)",
                }}
              />
              <span className={collapsed ? "sr-only" : "sidebar__agent-status-label"}>
                {statusLabel}
              </span>
            </div>
          );
        })}
      </div>
    </aside>
  );
}

export default TaskSidebar;
