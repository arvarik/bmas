"use client";

/**
 * PendingTaskContext — optimistic UI bridge for task submission.
 *
 * When the user submits a task on the landing page, there's a 150-400ms
 * gap between router.push(/task/{id}) and the task detail page receiving
 * its first SSE initial_state event. This context bridges that gap:
 *
 * 1. Landing page calls setPending({ taskId, inputText, submittedAt })
 * 2. Landing page calls router.push(`/task/${taskId}`)
 * 3. Task detail layout calls consumePending(taskId) on mount
 * 4. If pending exists, renders immediately with submitted text
 * 5. When SSE initial_state arrives, optimistic state is cleared
 *
 * Uses useRef (not useState) for the store to avoid re-renders in the
 * provider tree. The store is ephemeral — entries are consumed once
 * and have a ~500ms lifespan.
 *
 */

import React, { createContext, useContext, useRef, useCallback } from "react";

// ── Types ─────────────────────────────────────────────────────────────

export interface PendingTask {
  taskId: string;
  inputText: string;
  submittedAt: number;
}

interface PendingTaskCtx {
  setPending: (task: PendingTask) => void;
  consumePending: (taskId: string) => PendingTask | null;
}

// ── Context ───────────────────────────────────────────────────────────

const PendingTaskContext = createContext<PendingTaskCtx>({
  setPending: () => {},
  consumePending: () => null,
});

// ── Provider ──────────────────────────────────────────────────────────

export function PendingTaskProvider({
  children,
}: {
  children: React.ReactNode;
}) {
  const store = useRef<Map<string, PendingTask>>(new Map());

  const setPending = useCallback((task: PendingTask) => {
    store.current.set(task.taskId, task);
  }, []);

  // Consume-once: returns the pending task and removes it from the store.
  // Called by the task detail layout on mount.
  const consumePending = useCallback(
    (taskId: string): PendingTask | null => {
      const task = store.current.get(taskId) ?? null;
      if (task) store.current.delete(taskId);
      return task;
    },
    []
  );

  return (
    <PendingTaskContext.Provider value={{ setPending, consumePending }}>
      {children}
    </PendingTaskContext.Provider>
  );
}

// ── Hook ──────────────────────────────────────────────────────────────

export const usePendingTask = () => useContext(PendingTaskContext);
