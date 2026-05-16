"use client";

/**
 * TaskStreamContext — shared context for task detail views.
 *
 * The SSE stream is owned by the task layout and shared with all child
 * tab pages via this context. Child pages call useTaskData() to access
 * the stream data. They must NEVER call useTaskStream() directly.
 *
 * Extracted into its own module to avoid circular/sibling import issues
 * between layout.tsx and page.tsx in the App Router file convention.
 */

import { createContext, useContext } from "react";
import type { TaskStreamData } from "@/hooks/useTaskStream";

export const TaskStreamContext = createContext<TaskStreamData | null>(null);

/** Consume task stream data in child tab pages. */
export function useTaskData(): TaskStreamData {
  const ctx = useContext(TaskStreamContext);
  if (!ctx) throw new Error("useTaskData must be used within TaskLayout");
  return ctx;
}
