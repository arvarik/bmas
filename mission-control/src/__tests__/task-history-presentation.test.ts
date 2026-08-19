import { describe, expect, it } from "vitest";
import type { TaskSummary } from "@/hooks/useTaskHistory";
import {
  filterTaskHistory,
  parseSavedTaskViews,
  sortTaskHistory,
  taskHistoryCsv,
} from "@/lib/task-history-presentation";

function task(overrides: Partial<TaskSummary>): TaskSummary {
  return {
    id: "task-1",
    label: "Research",
    full_input: "Research the market",
    status: "completed",
    created_at: "2026-08-10T12:00:00Z",
    completed_at: "2026-08-10T12:01:00Z",
    total_cost_usd: 0.2,
    total_tokens: 500,
    duration_ms: 60_000,
    complexity: "medium",
    model_used: "model-a",
    error_message: null,
    run_state: null,
    ...overrides,
  };
}

const emptyFilters = {
  search: "",
  status: "",
  dateFrom: "",
  minCost: "",
  maxCost: "",
};

describe("task history presentation", () => {
  it("searches every loaded task field", () => {
    const tasks = [
      task({ id: "first", full_input: "Review alpha pricing" }),
      task({ id: "second", model_used: "edge-model" }),
      task({ id: "third", error_message: "Quota exceeded", status: "failed" }),
      task({ id: "fourth", result_summary: "Verified lighthouse report" }),
    ];

    expect(filterTaskHistory(tasks, { ...emptyFilters, search: "alpha" }).map((item) => item.id)).toEqual(["first"]);
    expect(filterTaskHistory(tasks, { ...emptyFilters, search: "edge-model" }).map((item) => item.id)).toEqual(["second"]);
    expect(filterTaskHistory(tasks, { ...emptyFilters, search: "quota" }).map((item) => item.id)).toEqual(["third"]);
    expect(filterTaskHistory(tasks, { ...emptyFilters, search: "lighthouse" }).map((item) => item.id)).toEqual(["fourth"]);
  });

  it("filters attention states and sorts the loaded results", () => {
    const tasks = [
      task({ id: "old", created_at: "2026-08-01T00:00:00Z", total_cost_usd: 2 }),
      task({ id: "paused", status: "running", run_state: "paused", total_cost_usd: 1 }),
      task({ id: "failed", status: "failed", total_cost_usd: 3 }),
      task({ id: "approval", pending_approval: true }),
      task({ id: "stale", stale: true }),
    ];
    const attention = filterTaskHistory(tasks, { ...emptyFilters, status: "attention" });

    expect(attention.map((item) => item.id)).toEqual(["paused", "failed", "approval", "stale"]);
    expect(sortTaskHistory(attention, "cost-high").map((item) => item.id)).toEqual(["failed", "paused", "approval", "stale"]);
  });

  it("exports filtered tasks as escaped CSV", () => {
    const csv = taskHistoryCsv([task({
      label: "Research, review",
      full_input: 'Say "hello"',
      error_message: "=CMD()",
    })]);

    expect(csv).toContain('"Research, review"');
    expect(csv).toContain('"Say ""hello"""');
    expect(csv).toContain('"\'=CMD()"');
  });

  it("rejects malformed saved views", () => {
    expect(parseSavedTaskViews("not json")).toEqual([]);
    expect(parseSavedTaskViews('[{"id":"one"}]')).toEqual([]);
  });

  it("reads a complete saved view", () => {
    const saved = JSON.stringify([{
      id: "view-one",
      name: "Recent failures",
      filters: { ...emptyFilters, status: "failed" },
      sort: "newest",
      datePreset: "7",
    }]);

    expect(parseSavedTaskViews(saved)).toHaveLength(1);
  });
});
