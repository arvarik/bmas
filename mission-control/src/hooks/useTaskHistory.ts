"use client";

/**
 * useTaskHistory — REST hook for task list with pagination.
 *
 * Fetches `GET /api/tasks?limit=50` on mount. Supports `loadMore()`
 * for offset-based pagination and `refetch()` for re-fetching when
 * the system stream emits task lifecycle events.
 *
 */

import { useState, useEffect, useCallback, useRef } from "react";

// ── Types ─────────────────────────────────────────────────────────────

export interface TaskSummary {
  id: string;
  label: string;
  full_input: string;
  status: "pending" | "running" | "completed" | "failed";
  created_at: string;
  completed_at: string | null;
  total_cost_usd: number;
  total_tokens: number;
  duration_ms: number | null;
  complexity: string | null;
  model_used: string | null;
  error_message: string | null;
  run_state: string | null;
  terminal_kind?: "completed" | "failed" | "cancelled" | null;
  failure_category?: string | null;
  result_summary?: string | null;
  last_heartbeat_at?: string | null;
  archived_at?: string | null;
  pending_approval?: boolean;
  stale?: boolean;
}

export interface TaskHistoryFilters {
  search: string;
  status: string;
  dateFrom: string;
  minCost: string;
  maxCost: string;
  archived?: "exclude" | "include" | "only";
  sort?: string;
}

export interface TaskHistoryData {
  tasks: TaskSummary[];
  total: number;
  grandTotal: number;
  isLoading: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => Promise<void>;
  refetch: () => Promise<void>;
}

// ── Constants ─────────────────────────────────────────────────────────

const PAGE_SIZE = 50;

// ── Hook ──────────────────────────────────────────────────────────────

export function useTaskHistory(
  filters: Partial<TaskHistoryFilters> = {},
): TaskHistoryData {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [grandTotal, setGrandTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const offsetRef = useRef(0);

  const search = filters.search?.trim() ?? "";
  const status = filters.status ?? "";
  const dateFrom = filters.dateFrom ?? "";
  const minCost = filters.minCost ?? "";
  const maxCost = filters.maxCost ?? "";
  const archived = filters.archived ?? "exclude";
  const sort = filters.sort ?? "created-desc";

  const fetchPage = useCallback(async (offset: number, append: boolean) => {
    setIsLoading(true);
    try {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
      });
      if (search) params.set("search", search);
      if (status) params.set("status", status);
      if (dateFrom) params.set("date_from", dateFrom);
      if (minCost) params.set("min_cost", minCost);
      if (maxCost) params.set("max_cost", maxCost);
      params.set("archived", archived);
      params.set("sort", sort);
      const res = await fetch(`/api/tasks?${params.toString()}`, {
        cache: "no-store",
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(
          (body as { error?: string }).error ?? `HTTP ${res.status}`
        );
      }

      const data = (await res.json()) as {
        tasks: TaskSummary[];
        total: number;
        grand_total?: number;
        limit: number;
        offset: number;
      };

      setTasks((prev) => (append ? [...prev, ...data.tasks] : data.tasks));
      setTotal(data.total);
      setGrandTotal(data.grand_total ?? data.total);
      setError(null);
      offsetRef.current = offset + data.tasks.length;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    } finally {
      setIsLoading(false);
    }
  }, [archived, dateFrom, maxCost, minCost, search, sort, status]);

  // Initial fetch on mount
  useEffect(() => {
    offsetRef.current = 0;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch, setState is in the callback not the effect body
    void fetchPage(0, false);
  }, [fetchPage]);

  // Load more (append next page)
  const loadMore = useCallback(async () => {
    await fetchPage(offsetRef.current, true);
  }, [fetchPage]);

  // Refetch from the beginning (called when system stream emits lifecycle event)
  const refetch = useCallback(async () => {
    offsetRef.current = 0;
    await fetchPage(0, false);
  }, [fetchPage]);

  const hasMore = tasks.length < total;

  return { tasks, total, grandTotal, isLoading, error, hasMore, loadMore, refetch };
}
