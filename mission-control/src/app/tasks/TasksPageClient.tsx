"use client";

/**
 * TasksPageClient — task history workspace.
 *
 * Layout:
 * 1. View row: status chips plus the operator's saved views.
 * 2. Search row: one search field, sort menu, archive menu, and a
 *    "Filters" toggle for the date and cost limits.
 * 3. Result bar: counts, selection, export, and archive actions.
 * 4. Result table.
 *
 * All filter state lives in the URL, so a view is a bookmarkable address.
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  Archive,
  ArchiveRestore,
  Bookmark,
  BookmarkPlus,
  Download,
  Pin,
  Plus,
  Search,
  SlidersHorizontal,
  X,
} from "lucide-react";
import { useTaskHistory, type TaskHistoryFilters, type TaskSummary } from "@/hooks/useTaskHistory";
import {
  parseSavedTaskViews,
  taskHistoryCsv,
  type SavedTaskView,
  type TaskHistorySort,
} from "@/lib/task-history-presentation";
import { ActionableError } from "@/components/ui/ActionableError";
import { SelectMenu } from "@/components/ui/SelectMenu";
import { useFocusTrap } from "@/hooks/useFocusTrap";

const VIEW_STORAGE_KEY = "bmas:task-views:v2";
const VIEW_EVENT = "bmas-task-views-changed";
const PIN_STORAGE_KEY = "bmas:pinned-tasks:v1";
const PIN_EVENT = "bmas-pins-changed";

const STATUS_VIEWS: { value: string; label: string }[] = [
  { value: "", label: "All" },
  { value: "attention", label: "Needs attention" },
  { value: "running", label: "Running" },
  { value: "pending", label: "Queued" },
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "cancelled", label: "Cancelled" },
];

const SORT_OPTIONS = [
  { value: "created-desc", label: "Newest created" },
  { value: "created-asc", label: "Oldest created" },
  { value: "activity-desc", label: "Latest activity" },
  { value: "duration-desc", label: "Longest duration" },
  { value: "duration-asc", label: "Shortest duration" },
  { value: "cost-desc", label: "Highest cost" },
  { value: "cost-asc", label: "Lowest cost" },
  { value: "status", label: "Status" },
];

const ARCHIVE_OPTIONS = [
  { value: "exclude", label: "Active history" },
  { value: "only", label: "Archived only" },
  { value: "include", label: "All tasks" },
];

function subscribeStorage(key: string, eventName: string, callback: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key === key) callback();
  };
  window.addEventListener("storage", onStorage);
  window.addEventListener(eventName, callback);
  return () => {
    window.removeEventListener("storage", onStorage);
    window.removeEventListener(eventName, callback);
  };
}

function downloadCsv(csv: string, name: string) {
  const url = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  URL.revokeObjectURL(url);
}

function taskStateLabel(task: TaskSummary): string {
  if (task.terminal_kind === "cancelled") return "Cancelled";
  if (task.pending_approval) return "Approval required";
  if (task.stale) return "Stale";
  if (["blocked", "paused", "pause_requested"].includes(task.run_state ?? "")) return "Blocked";
  if (task.status === "pending") return "Queued";
  return task.status.charAt(0).toUpperCase() + task.status.slice(1);
}

function taskStateTone(task: TaskSummary): string {
  if (task.terminal_kind === "cancelled") return "cancelled";
  if (task.pending_approval || task.stale || ["blocked", "paused", "pause_requested"].includes(task.run_state ?? "")) return "attention";
  return task.status;
}

function formatDuration(milliseconds: number | null): string {
  if (milliseconds === null) return "—";
  const seconds = Math.round(milliseconds / 1000);
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

function formatDateTime(iso: string): string {
  return new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

function viewSearchParams(view: SavedTaskView): URLSearchParams {
  const next = new URLSearchParams();
  if (view.filters.search) next.set("q", view.filters.search);
  if (view.filters.status) next.set("status", view.filters.status);
  if (view.filters.dateFrom) next.set("date_from", view.filters.dateFrom);
  if (view.filters.minCost) next.set("min_cost", view.filters.minCost);
  if (view.filters.maxCost) next.set("max_cost", view.filters.maxCost);
  if (view.filters.archived && view.filters.archived !== "exclude") next.set("archived", view.filters.archived);
  if (view.sort && view.sort !== "created-desc") next.set("sort", view.sort);
  return next;
}

export function TasksPageClient() {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const filters = useMemo<TaskHistoryFilters>(() => ({
    search: searchParams.get("q") ?? "",
    status: searchParams.get("status") ?? "",
    dateFrom: searchParams.get("date_from") ?? "",
    minCost: searchParams.get("min_cost") ?? "",
    maxCost: searchParams.get("max_cost") ?? "",
    archived: (searchParams.get("archived") as TaskHistoryFilters["archived"]) ?? "exclude",
    sort: searchParams.get("sort") ?? "created-desc",
  }), [searchParams]);
  const history = useTaskHistory(filters);
  const attentionHistory = useTaskHistory({ status: "attention" });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [archiveError, setArchiveError] = useState("");
  const [archivePending, setArchivePending] = useState(false);
  const [searchDraft, setSearchDraft] = useState(filters.search);
  const advancedActive = Boolean(filters.dateFrom || filters.minCost || filters.maxCost);
  const [advancedOpen, setAdvancedOpen] = useState(advancedActive);
  const [savePopoverOpen, setSavePopoverOpen] = useState(false);
  const [viewName, setViewName] = useState("");
  const savePopoverRef = useRef<HTMLDivElement>(null);
  const saveButtonRef = useRef<HTMLButtonElement>(null);
  const viewNameRef = useRef<HTMLInputElement>(null);

  const viewSnapshot = useSyncExternalStore(
    (callback) => subscribeStorage(VIEW_STORAGE_KEY, VIEW_EVENT, callback),
    () => window.localStorage.getItem(VIEW_STORAGE_KEY) ?? "[]",
    () => "[]",
  );
  const pinSnapshot = useSyncExternalStore(
    (callback) => subscribeStorage(PIN_STORAGE_KEY, PIN_EVENT, callback),
    () => window.localStorage.getItem(PIN_STORAGE_KEY) ?? "[]",
    () => "[]",
  );
  const savedViews = useMemo(() => parseSavedTaskViews(viewSnapshot), [viewSnapshot]);
  const pinnedIds = useMemo(() => {
    try {
      const parsed = JSON.parse(pinSnapshot) as unknown;
      return new Set(Array.isArray(parsed) ? parsed.filter((value): value is string => typeof value === "string") : []);
    } catch {
      return new Set<string>();
    }
  }, [pinSnapshot]);

  const updateUrl = useCallback((changes: Record<string, string>) => {
    const next = new URLSearchParams(searchParams.toString());
    for (const [key, value] of Object.entries(changes)) {
      if (value) next.set(key, value);
      else next.delete(key);
    }
    router.replace(`${pathname}${next.size ? `?${next.toString()}` : ""}`);
  }, [pathname, router, searchParams]);

  // Debounce the search field into the URL.
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- sync draft from URL changes (view chips, clear)
    setSearchDraft(filters.search);
  }, [filters.search]);
  useEffect(() => {
    if (searchDraft === filters.search) return;
    const timeout = window.setTimeout(() => updateUrl({ q: searchDraft }), 250);
    return () => window.clearTimeout(timeout);
  }, [filters.search, searchDraft, updateUrl]);

  useFocusTrap({
    active: savePopoverOpen,
    containerRef: savePopoverRef,
    initialFocusRef: viewNameRef,
    returnFocusRef: saveButtonRef,
    onEscape: () => setSavePopoverOpen(false),
  });

  useEffect(() => {
    if (!savePopoverOpen) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target as Node;
      if (savePopoverRef.current?.contains(target) || saveButtonRef.current?.contains(target)) return;
      setSavePopoverOpen(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [savePopoverOpen]);

  const currentQuery = searchParams.toString();
  const activeSavedView = useMemo(
    () => savedViews.find((view) => viewSearchParams(view).toString() === currentQuery && currentQuery !== ""),
    [currentQuery, savedViews],
  );

  const togglePin = (taskId: string) => {
    const next = new Set(pinnedIds);
    if (next.has(taskId)) next.delete(taskId);
    else next.add(taskId);
    window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify([...next]));
    window.dispatchEvent(new Event(PIN_EVENT));
  };

  const persistViews = (next: SavedTaskView[]) => {
    window.localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify(next));
    window.dispatchEvent(new Event(VIEW_EVENT));
  };

  const saveView = () => {
    const name = viewName.trim();
    if (!name) return;
    persistViews([
      ...savedViews.filter((view) => view.name.toLocaleLowerCase() !== name.toLocaleLowerCase()),
      {
        id: crypto.randomUUID(),
        name,
        filters,
        sort: filters.sort as TaskHistorySort,
        datePreset: "",
      },
    ]);
    setViewName("");
    setSavePopoverOpen(false);
  };

  const removeView = (viewId: string) => {
    persistViews(savedViews.filter((view) => view.id !== viewId));
  };

  const applyView = (view: SavedTaskView) => {
    const next = viewSearchParams(view);
    router.replace(`${pathname}${next.size ? `?${next.toString()}` : ""}`);
  };

  const exportTasks = () => {
    const rows = selected.size > 0
      ? history.tasks.filter((task) => selected.has(task.id))
      : history.tasks;
    downloadCsv(taskHistoryCsv(rows), "stigmergic-tasks.csv");
  };

  const archiveTasks = async () => {
    const restore = filters.archived === "only";
    const prompt = restore
      ? `Restore ${selected.size} selected task${selected.size === 1 ? "" : "s"} to active history?`
      : `Archive ${selected.size} selected task${selected.size === 1 ? "" : "s"}? Only completed and failed tasks can move to the archive.`;
    if (!selected.size || !window.confirm(prompt)) return;
    setArchivePending(true);
    setArchiveError("");
    try {
      const response = await fetch("/api/tasks/archive", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task_ids: [...selected], archived: !restore }),
      });
      const body = await response.json() as { error?: string; rejected?: string[] };
      if (!response.ok) throw new Error(body.error || `Archive returned HTTP ${response.status}`);
      if (body.rejected?.length) setArchiveError(`${body.rejected.length} active task${body.rejected.length === 1 ? " was" : "s were"} not archived.`);
      setSelected(new Set());
      await Promise.all([history.refetch(), attentionHistory.refetch()]);
    } catch (error) {
      setArchiveError(error instanceof Error ? error.message : "The archive request failed.");
    } finally {
      setArchivePending(false);
    }
  };

  const orderedTasks = useMemo(() => [...history.tasks].sort((left, right) => {
    return Number(pinnedIds.has(right.id)) - Number(pinnedIds.has(left.id));
  }), [history.tasks, pinnedIds]);

  const allVisibleSelected = orderedTasks.length > 0 && orderedTasks.every((task) => selected.has(task.id));
  const toggleAll = () => {
    setSelected((current) => {
      const next = new Set(current);
      if (allVisibleSelected) orderedTasks.forEach((task) => next.delete(task.id));
      else orderedTasks.forEach((task) => next.add(task.id));
      return next;
    });
  };

  const hasAnyFilter = currentQuery !== "";
  const advancedCount = [filters.dateFrom, filters.minCost, filters.maxCost].filter(Boolean).length;

  return (
    <div className="tasks-page">
      <header className="page-header">
        <div>
          <p className="page-eyebrow">Work</p>
          <h2>Tasks</h2>
        </div>
        <Link className="button button--primary" href="/"><Plus size={16} /> New task</Link>
      </header>

      {/* ── Views ──────────────────────────────────────────────────── */}
      <nav className="tasks-views" aria-label="Task views">
        <div className="tasks-views__chips" role="tablist" aria-label="Status">
          {STATUS_VIEWS.map((view) => {
            const active = filters.status === view.value;
            const count = view.value === "attention" ? attentionHistory.total : null;
            return (
              <button
                key={view.value || "all"}
                type="button"
                role="tab"
                aria-selected={active}
                className={`tasks-chip ${active ? "tasks-chip--active" : ""} ${view.value === "attention" && count ? "tasks-chip--attention" : ""}`}
                onClick={() => updateUrl({ status: view.value })}
              >
                {view.label}
                {count ? <span className="tasks-chip__count">{count > 99 ? "99+" : count}</span> : null}
              </button>
            );
          })}
        </div>

        <div className="tasks-views__saved">
          {savedViews.length > 0 ? <span className="tasks-views__divider" aria-hidden="true" /> : null}
          {savedViews.map((view) => {
            const active = activeSavedView?.id === view.id;
            return (
              <span key={view.id} className={`tasks-chip tasks-chip--saved ${active ? "tasks-chip--active" : ""}`}>
                <button type="button" className="tasks-chip__apply" onClick={() => applyView(view)} aria-pressed={active}>
                  <Bookmark size={12} aria-hidden="true" />
                  {view.name}
                </button>
                <button
                  type="button"
                  className="tasks-chip__remove"
                  aria-label={`Delete saved view ${view.name}`}
                  onClick={() => removeView(view.id)}
                >
                  <X size={12} aria-hidden="true" />
                </button>
              </span>
            );
          })}
          <div className="tasks-save">
            <button
              ref={saveButtonRef}
              type="button"
              className="tasks-chip tasks-chip--ghost"
              onClick={() => setSavePopoverOpen((open) => !open)}
              disabled={!hasAnyFilter}
              title={hasAnyFilter ? "Save the current filters as a view" : "Apply a filter first, then save it as a view"}
              aria-expanded={savePopoverOpen}
              aria-haspopup="dialog"
            >
              <BookmarkPlus size={14} aria-hidden="true" />
              Save view
            </button>
            {savePopoverOpen ? (
              <div ref={savePopoverRef} className="tasks-save__popover" role="dialog" aria-label="Save view">
                <label htmlFor="tasks-view-name">View name</label>
                <form
                  onSubmit={(event) => {
                    event.preventDefault();
                    saveView();
                  }}
                >
                  <input
                    ref={viewNameRef}
                    id="tasks-view-name"
                    value={viewName}
                    onChange={(event) => setViewName(event.target.value)}
                    placeholder="For example, costly failures"
                    maxLength={60}
                  />
                  <button type="submit" className="button button--primary" disabled={!viewName.trim()}>Save</button>
                </form>
                <p>Saves the current search, status, sort, and limits in this browser.</p>
              </div>
            ) : null}
          </div>
        </div>
      </nav>

      {/* ── Search and sort ────────────────────────────────────────── */}
      <section className="tasks-toolbar" aria-label="Search and sort">
        <label className="tasks-search">
          <Search size={16} aria-hidden="true" />
          <span className="sr-only">Search tasks</span>
          <input
            type="search"
            value={searchDraft}
            onChange={(event) => setSearchDraft(event.target.value)}
            placeholder="Search objective, result, error, or task ID"
          />
          {searchDraft ? (
            <button type="button" className="tasks-search__clear" aria-label="Clear search" onClick={() => setSearchDraft("")}>
              <X size={14} aria-hidden="true" />
            </button>
          ) : null}
        </label>
        <SelectMenu
          aria-label="Sort"
          value={filters.sort ?? "created-desc"}
          options={SORT_OPTIONS}
          prefix={<span className="tasks-toolbar__prefix">Sort</span>}
          onChange={(value) => updateUrl({ sort: value === "created-desc" ? "" : value })}
        />
        <SelectMenu
          aria-label="Archive"
          value={filters.archived ?? "exclude"}
          options={ARCHIVE_OPTIONS}
          prefix={<span className="tasks-toolbar__prefix">Show</span>}
          onChange={(value) => updateUrl({ archived: value === "exclude" ? "" : value })}
        />
        <button
          type="button"
          className={`button ${advancedOpen || advancedCount ? "button--active" : ""}`}
          onClick={() => setAdvancedOpen((open) => !open)}
          aria-expanded={advancedOpen}
          aria-controls="tasks-advanced-filters"
        >
          <SlidersHorizontal size={15} aria-hidden="true" /> Filters{advancedCount ? <span className="tasks-chip__count">{advancedCount}</span> : null}
        </button>
        {hasAnyFilter ? (
          <button type="button" className="tasks-toolbar__clear" onClick={() => router.replace(pathname)}>Clear</button>
        ) : null}
      </section>

      {advancedOpen ? (
        <section id="tasks-advanced-filters" className="tasks-advanced" aria-label="Limits">
          <label>
            <span>Created after</span>
            <input type="date" value={filters.dateFrom} onChange={(event) => updateUrl({ date_from: event.target.value })} />
          </label>
          <label>
            <span>Minimum cost</span>
            <span className="tasks-advanced__money">
              <span aria-hidden="true">$</span>
              <input type="number" min="0" step="0.01" inputMode="decimal" value={filters.minCost} onChange={(event) => updateUrl({ min_cost: event.target.value })} placeholder="0.00" />
            </span>
          </label>
          <label>
            <span>Maximum cost</span>
            <span className="tasks-advanced__money">
              <span aria-hidden="true">$</span>
              <input type="number" min="0" step="0.01" inputMode="decimal" value={filters.maxCost} onChange={(event) => updateUrl({ max_cost: event.target.value })} placeholder="No limit" />
            </span>
          </label>
          {advancedCount ? (
            <button type="button" className="tasks-toolbar__clear" onClick={() => updateUrl({ date_from: "", min_cost: "", max_cost: "" })}>Reset limits</button>
          ) : null}
        </section>
      ) : null}

      {/* ── Result bar ─────────────────────────────────────────────── */}
      <div className="tasks-results">
        <span className="tasks-results__count">
          {history.isLoading && !history.tasks.length
            ? "Loading…"
            : `${history.tasks.length} of ${history.total} task${history.total === 1 ? "" : "s"}`}
          {selected.size ? <> · <strong>{selected.size} selected</strong></> : null}
          {pinnedIds.size ? <> · {pinnedIds.size} pinned</> : null}
        </span>
        <div className="tasks-results__actions">
          {selected.size ? (
            <button type="button" className="tasks-toolbar__clear" onClick={() => setSelected(new Set())}>Clear selection</button>
          ) : null}
          <button type="button" className="button" onClick={exportTasks} disabled={!history.tasks.length}>
            <Download size={15} aria-hidden="true" /> Export{selected.size ? ` ${selected.size}` : ""}
          </button>
          <button type="button" className="button" onClick={() => void archiveTasks()} disabled={!selected.size || archivePending}>
            {filters.archived === "only" ? <ArchiveRestore size={15} aria-hidden="true" /> : <Archive size={15} aria-hidden="true" />}
            {archivePending ? "Updating…" : filters.archived === "only" ? "Restore" : "Archive"}{selected.size ? ` ${selected.size}` : ""}
          </button>
        </div>
      </div>

      {history.error ? <ActionableError component="Task history" cause={history.error} onRetry={() => void history.refetch()} /> : null}
      {archiveError ? <ActionableError component="Task archive" cause={archiveError} compact /> : null}

      <div className="tasks-table-wrap">
        <table className="tasks-table">
          <caption className="sr-only">Task history results</caption>
          <thead>
            <tr>
              <th scope="col">
                <label className="task-selection-control">
                  <input
                    type="checkbox"
                    aria-label="Select all visible tasks"
                    checked={allVisibleSelected}
                    onChange={toggleAll}
                    disabled={!orderedTasks.length}
                  />
                </label>
              </th>
              <th scope="col">Task</th>
              <th scope="col">State</th>
              <th scope="col">Created</th>
              <th scope="col">Duration</th>
              <th scope="col">Cost</th>
              <th scope="col"><span className="sr-only">Pin</span></th>
            </tr>
          </thead>
          <tbody>
            {orderedTasks.map((task) => (
              <tr key={task.id} className={selected.has(task.id) ? "tasks-table__row--selected" : ""}>
                <td>
                  <label className="task-selection-control">
                    <input
                      type="checkbox"
                      aria-label={`Select ${task.label}`}
                      checked={selected.has(task.id)}
                      onChange={(event) => setSelected((current) => {
                        const next = new Set(current);
                        if (event.target.checked) next.add(task.id);
                        else next.delete(task.id);
                        return next;
                      })}
                    />
                  </label>
                </td>
                <td>
                  <Link href={`/task/${task.id}`} className="tasks-table__task">
                    <strong>{task.label}</strong>
                    <small>{task.id}{task.complexity ? ` · ${task.complexity}` : ""}{task.model_used ? ` · ${task.model_used}` : ""}</small>
                  </Link>
                </td>
                <td><span className={`task-state-label task-state-label--${taskStateTone(task)}`}>{taskStateLabel(task)}</span></td>
                <td><time dateTime={task.created_at}>{formatDateTime(task.created_at)}</time></td>
                <td>{formatDuration(task.duration_ms)}</td>
                <td className="tasks-table__cost">${task.total_cost_usd.toFixed(4)}</td>
                <td>
                  <button
                    type="button"
                    className={`icon-button tasks-table__pin ${pinnedIds.has(task.id) ? "tasks-table__pin--active" : ""}`}
                    onClick={() => togglePin(task.id)}
                    aria-pressed={pinnedIds.has(task.id)}
                    aria-label={`${pinnedIds.has(task.id) ? "Unpin" : "Pin"} ${task.label}`}
                  >
                    <Pin size={15} fill={pinnedIds.has(task.id) ? "currentColor" : "none"} />
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {history.isLoading && !history.tasks.length ? <p className="tasks-table__status" role="status">Loading task results…</p> : null}
        {!history.isLoading && !history.tasks.length && !history.error ? (
          <div className="tasks-empty">
            <h3>{hasAnyFilter ? "No tasks match these filters" : "No tasks yet"}</h3>
            <p>{hasAnyFilter ? "Change the filters or clear them." : "Submit a task from the home page to start."}</p>
            {hasAnyFilter ? <button type="button" className="button" onClick={() => router.replace(pathname)}>Clear filters</button> : null}
          </div>
        ) : null}
      </div>
      {history.hasMore ? <button type="button" className="button tasks-load-more" onClick={() => void history.loadMore()} disabled={history.isLoading}>Load more</button> : null}
    </div>
  );
}
