"use client";

/**
 * Task Detail Layout — wraps all /task/[taskId]/* pages.
 *
 * This layout owns the SSE stream connection via useTaskStream() and
 * distributes data to child tab pages through TaskStreamContext. This
 * ensures tab switches are instantaneous DOM swaps — no SSE reconnection.
 *
 */

import React, { useEffect, useRef, useState } from "react";
import { useParams, useSelectedLayoutSegment } from "next/navigation";
import Link from "next/link";
import { useTaskStream } from "@/hooks/useTaskStream";
import type { TaskStreamData, CostData } from "@/hooks/useTaskStream";
import { TaskStreamContext } from "./TaskStreamContext";
import { usePendingTask, type PendingTask } from "@/contexts/PendingTaskContext";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { TaskLifecycle } from "@/components/features/TaskLifecycle";
import { ActionableError } from "@/components/ui/ActionableError";
import type { StatusType } from "@/lib/design-tokens";
import { ArrowLeft } from "lucide-react";
import { UnsupportedVariantState } from "@/components/features/UnsupportedVariantState";
import { getActiveAdapter, visibleNavigationPanels } from "@/lib/variants";

// Re-export useTaskData for convenience (child pages should import
// from TaskStreamContext.tsx directly, but this keeps backward compat).
export { useTaskData } from "./TaskStreamContext";

// ── Status mapping ───────────────────────────────────────────────────

const STATUS_MAP: Record<string, StatusType> = {
  pending: "pending",
  running: "running",
  completed: "success",
  failed: "error",
};

// ── Task Header ──────────────────────────────────────────────────────

function TaskHeader({
  taskMeta,
  isLive,
  pending,
  cost,
}: {
  taskMeta: TaskStreamData["taskMeta"];
  isLive: boolean;
  pending: PendingTask | null;
  cost: CostData | null;
}) {
  // Use optimistic data when SSE hasn't delivered real meta yet
  const hasMeta = !!taskMeta;
  const status = taskMeta?.status ?? (isLive ? "running" : "pending");
  const runState = taskMeta?.run_state ?? "";
  const blocked = ["blocked", "paused", "pause_requested"].includes(runState);
  const statusType = blocked ? "paused" : STATUS_MAP[status] ?? "pending";
  const label = hasMeta
    ? (taskMeta?.label ?? "Loading…")
    : pending
      ? pending.inputText.slice(0, 80)
      : "Loading…";

  // Duration display
  const durationText = taskMeta?.duration_ms
    ? fmtDuration(taskMeta.duration_ms)
    : undefined;

  return (
    <div className="task-header">
      <Link href="/" className="task-header__back">
        <ArrowLeft size={16} />
        <span>Back</span>
      </Link>
      <h2 className="task-header__title">{label}</h2>
      <div className="task-header__meta">
        {!hasMeta && pending ? (
          <StatusBadge status="running" label="Awakening Swarm…" />
        ) : (
          <StatusBadge
            status={statusType}
            label={
              blocked
                ? runState === "paused" ? "Paused" : "Blocked"
                : isLive
                ? "Running"
                : status === "completed"
                  ? "Completed"
                  : status === "failed"
                    ? "Failed"
                    : "Pending"
            }
          />
        )}
        {taskMeta?.complexity && (
          <span
            className="task-header__badge"
            title={`Triage complexity: ${taskMeta.complexity} — The AI router classified this prompt's difficulty to select the appropriate model tier`}
          >
            {taskMeta.complexity}
          </span>
        )}
        {taskMeta?.variant && (
          <span
            className="task-header__badge variant-chip"
            title={`Coordination variant: ${taskMeta.variant}`}
          >
            {taskMeta.variant}
          </span>
        )}
        {taskMeta?.model && (
          <span className="task-header__model">{taskMeta.model}</span>
        )}
        {cost && (
          <span className="task-header__cost">
            ${cost.total_cost.toFixed(4)}
          </span>
        )}
        {durationText && (
          <span className="task-header__duration">{durationText}</span>
        )}
      </div>
    </div>
  );
}

function fmtDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return `${m}m ${rem}s`;
}

// ── Layout ───────────────────────────────────────────────────────────

export default function TaskLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const { taskId } = useParams();
  const segment = useSelectedLayoutSegment();
  const basePath = `/task/${taskId}`;

  // ── Optimistic state from PendingTaskContext ──────────────────────
  const { consumePending } = usePendingTask();
  const [pending, setPending] = useState<PendingTask | null>(null);
  const consumed = useRef(false);

  useEffect(() => {
    if (!consumed.current) {
      consumed.current = true;
      const p = consumePending(taskId as string);
      // eslint-disable-next-line react-hooks/set-state-in-effect -- one-time consumption on mount
      if (p) setPending(p);
    }
  }, [taskId, consumePending]);

  // ── SSE stream lives here (layout persists across tab switches) ──
  const streamData = useTaskStream(taskId as string);
  const adapter = getActiveAdapter(streamData.runtime.adapterId);
  const capability = streamData.runtime.capability;
  const tabs = adapter && capability
    ? visibleNavigationPanels(adapter, capability)
    : [];
  const requestedPanel = adapter?.navigationPanels.find((panel) => (
    panel.segment === segment || (segment === "artifacts" && panel.segment === "files")
  ));
  const panelAvailable = tabs.some((panel) => (
    panel.segment === segment || (segment === "artifacts" && panel.segment === "files")
  ));

  // Clear optimistic state when real data arrives
  useEffect(() => {
    if (streamData.taskMeta && pending) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- clearing optimistic state when real data arrives
      setPending(null);
    }
  }, [streamData.taskMeta, pending]);

  return (
    <TaskStreamContext.Provider value={streamData}>
      <div className="task-detail">
        <TaskHeader
          taskMeta={streamData.taskMeta}
          isLive={streamData.isLive}
          pending={pending}
          cost={streamData.cost}
        />
        <TaskLifecycle
          task={streamData.taskMeta}
          cost={streamData.cost}
          isLive={streamData.isLive}
          isPaused={streamData.isPaused}
          controls={capability?.features.controls ?? []}
        />
        {tabs.length > 0 ? <nav className="task-tabs" role="tablist">
          {tabs.map((tab) => (
            <Link
              key={tab.label}
              href={tab.segment ? `${basePath}/${tab.segment}` : basePath}
              className={`task-tabs__tab ${segment === tab.segment ? "task-tabs__tab--active" : ""}`}
              role="tab"
              aria-selected={segment === tab.segment}
            >
              {tab.label}
            </Link>
          ))}
        </nav> : null}
        <div className="task-content">
          {streamData.runtime.status === "ready" && streamData.hydrationError ? (
            <ActionableError
              component="Saved task data"
              cause={streamData.hydrationError}
              compact
            />
          ) : null}
          {streamData.runtime.status !== "ready" ? (
            <UnsupportedVariantState runtime={streamData.runtime} />
          ) : panelAvailable ? children : (
            <UnsupportedVariantState
              runtime={streamData.runtime}
              feature={requestedPanel?.label ?? String(segment ?? "overview")}
            />
          )}
        </div>
      </div>
    </TaskStreamContext.Provider>
  );
}
