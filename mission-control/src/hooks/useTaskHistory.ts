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
}

export interface TaskHistoryData {
  tasks: TaskSummary[];
  total: number;
  isLoading: boolean;
  error: string | null;
  hasMore: boolean;
  loadMore: () => Promise<void>;
  refetch: () => Promise<void>;
}

// ── Constants ─────────────────────────────────────────────────────────

const PAGE_SIZE = 50;

// ── Hook ──────────────────────────────────────────────────────────────

export function useTaskHistory(): TaskHistoryData {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const offsetRef = useRef(0);

  const fetchPage = useCallback(async (offset: number, append: boolean) => {
    setIsLoading(true);
    try {
      const res = await fetch(`/api/tasks?limit=${PAGE_SIZE}&offset=${offset}`, {
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
        limit: number;
        offset: number;
      };

      setTasks((prev) => (append ? [...prev, ...data.tasks] : data.tasks));
      setTotal(data.total);
      setError(null);
      offsetRef.current = offset + data.tasks.length;
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load tasks");
    } finally {
      setIsLoading(false);
    }
  }, []);

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

  return { tasks, total, isLoading, error, hasMore, loadMore, refetch };
}
