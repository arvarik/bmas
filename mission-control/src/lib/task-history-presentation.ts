import type { TaskHistoryFilters, TaskSummary } from "@/hooks/useTaskHistory";

export type TaskHistorySort =
  | "newest"
  | "oldest"
  | "cost-high"
  | "cost-low"
  | "duration-high";

export interface SavedTaskView {
  id: string;
  name: string;
  filters: TaskHistoryFilters;
  sort: TaskHistorySort;
  datePreset: "" | "0" | "7" | "30";
}

export function taskNeedsAttention(task: TaskSummary): boolean {
  return task.status === "failed"
    || ["blocked", "paused", "pause_requested"].includes(task.run_state ?? "");
}

function matchesSearch(task: TaskSummary, search: string): boolean {
  const query = search.trim().toLocaleLowerCase();
  if (!query) return true;
  return [
    task.id,
    task.label,
    task.full_input,
    task.status,
    task.run_state,
    task.complexity,
    task.model_used,
    task.error_message,
  ].some((value) => value?.toLocaleLowerCase().includes(query));
}

export function filterTaskHistory(
  tasks: readonly TaskSummary[],
  filters: TaskHistoryFilters,
): TaskSummary[] {
  const dateFrom = filters.dateFrom ? Date.parse(filters.dateFrom) : null;
  const minimumCost = filters.minCost ? Number(filters.minCost) : null;
  const maximumCost = filters.maxCost ? Number(filters.maxCost) : null;

  return tasks.filter((task) => {
    if (!matchesSearch(task, filters.search)) return false;
    if (filters.status === "attention" && !taskNeedsAttention(task)) return false;
    if (filters.status && filters.status !== "attention" && task.status !== filters.status) {
      return false;
    }
    if (dateFrom !== null && new Date(task.created_at).getTime() < dateFrom) {
      return false;
    }
    if (minimumCost !== null && task.total_cost_usd < minimumCost) return false;
    if (maximumCost !== null && task.total_cost_usd > maximumCost) return false;
    return true;
  });
}

export function sortTaskHistory(
  tasks: readonly TaskSummary[],
  sort: TaskHistorySort,
): TaskSummary[] {
  const sorted = [...tasks];
  sorted.sort((left, right) => {
    if (sort === "oldest") {
      return Date.parse(left.created_at) - Date.parse(right.created_at);
    }
    if (sort === "cost-high") {
      return right.total_cost_usd - left.total_cost_usd;
    }
    if (sort === "cost-low") {
      return left.total_cost_usd - right.total_cost_usd;
    }
    if (sort === "duration-high") {
      return (right.duration_ms ?? -1) - (left.duration_ms ?? -1);
    }
    return Date.parse(right.created_at) - Date.parse(left.created_at);
  });
  return sorted;
}

export function prepareTaskHistory(
  tasks: readonly TaskSummary[],
  filters: TaskHistoryFilters,
  sort: TaskHistorySort,
): TaskSummary[] {
  return sortTaskHistory(filterTaskHistory(tasks, filters), sort);
}

function escapeCsv(value: string | number | null): string {
  const rawText = value === null ? "" : String(value);
  const text = /^[=+\-@]/.test(rawText) ? `'${rawText}` : rawText;
  return `"${text.replaceAll('"', '""')}"`;
}

export function taskHistoryCsv(tasks: readonly TaskSummary[]): string {
  const columns = [
    "id",
    "label",
    "status",
    "run_state",
    "created_at",
    "completed_at",
    "cost_usd",
    "tokens",
    "duration_ms",
    "complexity",
    "model",
    "input",
    "error",
  ];
  const rows = tasks.map((task) => [
    task.id,
    task.label,
    task.status,
    task.run_state,
    task.created_at,
    task.completed_at,
    task.total_cost_usd,
    task.total_tokens,
    task.duration_ms,
    task.complexity,
    task.model_used,
    task.full_input,
    task.error_message,
  ].map(escapeCsv).join(","));
  return [columns.join(","), ...rows].join("\n");
}

export function parseSavedTaskViews(value: string): SavedTaskView[] {
  try {
    const parsed = JSON.parse(value) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((entry): entry is SavedTaskView => {
      if (typeof entry !== "object" || entry === null) return false;
      const view = entry as Partial<SavedTaskView>;
      const filters = view.filters as Partial<TaskHistoryFilters> | undefined;
      if (!filters) return false;
      return typeof view.id === "string"
        && typeof view.name === "string"
        && view.name.trim().length > 0
        && typeof filters?.search === "string"
        && typeof filters.status === "string"
        && typeof filters.dateFrom === "string"
        && typeof filters.minCost === "string"
        && typeof filters.maxCost === "string"
        && ["", "0", "7", "30"].includes(view.datePreset ?? "invalid")
        && ["newest", "oldest", "cost-high", "cost-low", "duration-high"].includes(view.sort ?? "");
    });
  } catch {
    return [];
  }
}
