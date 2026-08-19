"use client";

import { useCallback, useMemo, useState, useSyncExternalStore } from "react";
import Link from "next/link";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { Archive, ArchiveRestore, Download, Pin, Plus, Save, Search } from "lucide-react";
import { useTaskHistory, type TaskHistoryFilters, type TaskSummary } from "@/hooks/useTaskHistory";
import {
  parseSavedTaskViews,
  taskHistoryCsv,
  type SavedTaskView,
  type TaskHistorySort,
} from "@/lib/task-history-presentation";
import { ActionableError } from "@/components/ui/ActionableError";

const VIEW_STORAGE_KEY = "bmas:task-views:v2";
const VIEW_EVENT = "bmas-task-views-changed";
const PIN_STORAGE_KEY = "bmas:pinned-tasks:v1";
const PIN_EVENT = "bmas-pins-changed";

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
  if (task.pending_approval) return "Approval required";
  if (task.stale) return "Stale";
  if (["blocked", "paused", "pause_requested"].includes(task.run_state ?? "")) return "Blocked";
  if (task.status === "pending") return "Queued";
  return task.status.charAt(0).toUpperCase() + task.status.slice(1);
}

function formatDuration(milliseconds: number | null): string {
  if (milliseconds === null) return "—";
  const seconds = Math.round(milliseconds / 1000);
  return seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
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
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [viewName, setViewName] = useState("");
  const [archiveError, setArchiveError] = useState("");
  const [archivePending, setArchivePending] = useState(false);

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

  const togglePin = (taskId: string) => {
    const next = new Set(pinnedIds);
    if (next.has(taskId)) next.delete(taskId);
    else next.add(taskId);
    window.localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify([...next]));
    window.dispatchEvent(new Event(PIN_EVENT));
  };

  const saveView = () => {
    const name = viewName.trim();
    if (!name) return;
    const next: SavedTaskView[] = [
      ...savedViews.filter((view) => view.name.toLocaleLowerCase() !== name.toLocaleLowerCase()),
      {
        id: crypto.randomUUID(),
        name,
        filters,
        sort: filters.sort as TaskHistorySort,
        datePreset: "",
      },
    ];
    window.localStorage.setItem(VIEW_STORAGE_KEY, JSON.stringify(next));
    window.dispatchEvent(new Event(VIEW_EVENT));
    setViewName("");
  };

  const applyView = (viewId: string) => {
    const view = savedViews.find((candidate) => candidate.id === viewId);
    if (!view) return;
    const next = new URLSearchParams();
    if (view.filters.search) next.set("q", view.filters.search);
    if (view.filters.status) next.set("status", view.filters.status);
    if (view.filters.dateFrom) next.set("date_from", view.filters.dateFrom);
    if (view.filters.minCost) next.set("min_cost", view.filters.minCost);
    if (view.filters.maxCost) next.set("max_cost", view.filters.maxCost);
    if (view.filters.archived && view.filters.archived !== "exclude") next.set("archived", view.filters.archived);
    if (view.sort !== "created-desc") next.set("sort", view.sort);
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
      await history.refetch();
    } catch (error) {
      setArchiveError(error instanceof Error ? error.message : "The archive request failed.");
    } finally {
      setArchivePending(false);
    }
  };

  const orderedTasks = useMemo(() => [...history.tasks].sort((left, right) => {
    return Number(pinnedIds.has(right.id)) - Number(pinnedIds.has(left.id));
  }), [history.tasks, pinnedIds]);

  return (
    <div className="tasks-page">
      <header className="page-header">
        <div>
          <p className="page-eyebrow">Work</p>
          <h2>{filters.status === "attention" ? "Needs attention" : "Tasks"}</h2>
          <p>Search the full task objective and result. Filter state stays in the URL.</p>
        </div>
        <Link className="button button--primary" href="/"><Plus size={16} /> New task</Link>
      </header>

      <section className="task-history-toolbar" aria-label="Task filters">
        <label className="task-history-search">
          <span>Search</span>
          <span><Search size={15} aria-hidden="true" /><input value={filters.search} onChange={(event) => updateUrl({ q: event.target.value })} placeholder="Objective, result, error, task ID…" /></span>
        </label>
        <label>Status<select value={filters.status} onChange={(event) => updateUrl({ status: event.target.value })}>
          <option value="">All states</option><option value="attention">Needs attention</option><option value="pending">Queued</option><option value="running">Running</option><option value="completed">Completed</option><option value="failed">Failed</option>
        </select></label>
        <label>Sort<select value={filters.sort} onChange={(event) => updateUrl({ sort: event.target.value === "created-desc" ? "" : event.target.value })}>
          <option value="created-desc">Newest created</option><option value="created-asc">Oldest created</option><option value="activity-desc">Latest activity</option><option value="duration-desc">Longest duration</option><option value="duration-asc">Shortest duration</option><option value="cost-desc">Highest cost</option><option value="cost-asc">Lowest cost</option><option value="status">Status</option>
        </select></label>
        <label>Archive<select value={filters.archived} onChange={(event) => updateUrl({ archived: event.target.value === "exclude" ? "" : event.target.value })}>
          <option value="exclude">Active history</option><option value="only">Archived only</option><option value="include">All tasks</option>
        </select></label>
        <label>Created after<input type="date" value={filters.dateFrom} onChange={(event) => updateUrl({ date_from: event.target.value })} /></label>
        <label>Minimum cost<input type="number" min="0" step="0.01" inputMode="decimal" value={filters.minCost} onChange={(event) => updateUrl({ min_cost: event.target.value })} placeholder="$0.00" /></label>
        <label>Maximum cost<input type="number" min="0" step="0.01" inputMode="decimal" value={filters.maxCost} onChange={(event) => updateUrl({ max_cost: event.target.value })} placeholder="No maximum" /></label>
        <button type="button" className="button" onClick={() => router.replace(pathname)}>Clear filters</button>
      </section>

      <section className="task-saved-views" aria-label="Saved views">
        <label>Saved view<select defaultValue="" onChange={(event) => applyView(event.target.value)}><option value="">Choose a view</option>{savedViews.map((view) => <option key={view.id} value={view.id}>{view.name}</option>)}</select></label>
        <label>View name<input value={viewName} onChange={(event) => setViewName(event.target.value)} placeholder="For example, costly failures" /></label>
        <button type="button" onClick={saveView} disabled={!viewName.trim()}><Save size={15} /> Save view</button>
      </section>

      <div className="task-history-summary">
        <span>Showing {history.tasks.length} of {history.total} matching tasks</span>
        <span>{history.grandTotal} active tasks in total</span>
        <span>{pinnedIds.size} browser-local pin{pinnedIds.size === 1 ? "" : "s"}</span>
        <div>
          <button type="button" onClick={exportTasks} disabled={!history.tasks.length}><Download size={15} /> Export {selected.size ? "selection" : "visible tasks"}</button>
          <button type="button" onClick={() => void archiveTasks()} disabled={!selected.size || archivePending}>{filters.archived === "only" ? <ArchiveRestore size={15} /> : <Archive size={15} />} {archivePending ? "Updating…" : filters.archived === "only" ? "Restore selection" : "Archive selection"}</button>
        </div>
      </div>

      {history.error ? <ActionableError component="Task history" cause={history.error} onRetry={() => void history.refetch()} /> : null}
      {archiveError ? <ActionableError component="Task archive" cause={archiveError} compact /> : null}

      <div className="task-history-table-wrap">
        <table className="task-history-table">
          <caption className="sr-only">Task history results</caption>
          <thead><tr><th scope="col"><span className="sr-only">Select</span></th><th scope="col">Task</th><th scope="col">State</th><th scope="col">Created</th><th scope="col">Activity</th><th scope="col">Duration</th><th scope="col">Cost</th><th scope="col">Pin</th></tr></thead>
          <tbody>
            {orderedTasks.map((task) => (
              <tr key={task.id}>
                <td><label className="task-selection-control"><input type="checkbox" aria-label={`Select ${task.label}`} checked={selected.has(task.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(task.id); else next.delete(task.id); return next; })} /></label></td>
                <td><Link href={`/task/${task.id}`}><strong>{task.label}</strong><small>{task.id}</small></Link></td>
                <td><span className={`task-state-label task-state-label--${task.status}`}>{taskStateLabel(task)}</span></td>
                <td><time dateTime={task.created_at}>{new Date(task.created_at).toLocaleString()}</time></td>
                <td>{task.last_heartbeat_at ? <time dateTime={task.last_heartbeat_at}>{new Date(task.last_heartbeat_at).toLocaleString()}</time> : "—"}</td>
                <td>{formatDuration(task.duration_ms)}</td>
                <td>${task.total_cost_usd.toFixed(4)}</td>
                <td><button type="button" className="icon-button" onClick={() => togglePin(task.id)} aria-pressed={pinnedIds.has(task.id)} aria-label={`${pinnedIds.has(task.id) ? "Remove" : "Add"} browser-local pin for ${task.label}`}><Pin size={15} fill={pinnedIds.has(task.id) ? "currentColor" : "none"} /></button></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {history.isLoading ? <p role="status">Loading task results…</p> : null}
      {!history.isLoading && !history.tasks.length && !history.error ? <div className="empty-state"><h3>No matching tasks</h3><p>Change the filters or create a new task.</p></div> : null}
      {history.hasMore ? <button type="button" className="button" onClick={() => void history.loadMore()} disabled={history.isLoading}>Load more</button> : null}
    </div>
  );
}
