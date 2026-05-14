"use client";

import dynamic from "next/dynamic";
import { useBlackboard, selectTasks, type Task } from "@/hooks/useBlackboard";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { Skeleton } from "@/components/ui/Skeleton";
import type { StatusType } from "@/lib/design-tokens";
import { GitBranch, ChevronRight, List } from "lucide-react";
import { useState } from "react";

const DAGVisualizer = dynamic(() => import("@/components/DAGVisualizer"), {
  ssr: false,
  loading: () => (
    <div style={{ height: "100%", display: "flex", alignItems: "center", justifyContent: "center", background: "var(--surface-overlay)", borderRadius: "var(--radius-lg)" }}>
      <Skeleton variant="dag" />
    </div>
  ),
});

const STATUS_MAP: Record<string, StatusType> = {
  pending: "pending", running: "running", completed: "success", failed: "error",
};

function TaskListPanel({ tasks }: { tasks: Task[] }) {
  const sorted = [...tasks].sort(
    (a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime()
  );

  if (sorted.length === 0) {
    return (
      <div className="dag-tasklist-empty">
        <GitBranch size={24} style={{ color: "var(--text-tertiary)" }} />
        <span>No tasks submitted</span>
      </div>
    );
  }

  return (
    <div className="dag-tasklist-items">
      {sorted.map((t) => (
        <div key={t.id} className="dag-tasklist-item">
          <StatusBadge status={STATUS_MAP[t.status] ?? "pending"} />
          <div className="dag-tasklist-item__info">
            <span className="dag-tasklist-item__label">{t.label}</span>
            <span className="dag-tasklist-item__id">{t.id}</span>
          </div>
          <ChevronRight size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
        </div>
      ))}
    </div>
  );
}

export default function DAGView() {
  const tasks = useBlackboard(selectTasks);
  const [showList, setShowList] = useState(true);

  return (
    <div className="view-container dag-view">
      {/* Mobile: task summary above DAG */}
      <div className="dag-mobile-summary">
        <div className="dag-mobile-summary__header">
          <GitBranch size={16} />
          <span>Task DAG</span>
          <span className="dag-mobile-summary__count">{tasks.length} tasks</span>
        </div>
      </div>

      <div className="dag-layout">
        {/* DAG Canvas */}
        <div className="dag-canvas">
          <DAGVisualizer />
        </div>

        {/* Task list sidebar — desktop only */}
        <div className={`dag-sidebar ${showList ? "dag-sidebar--open" : "dag-sidebar--closed"}`}>
          <button
            className="dag-sidebar__toggle"
            onClick={() => setShowList((v) => !v)}
            title={showList ? "Hide task list" : "Show task list"}
          >
            <List size={16} />
          </button>
          {showList && (
            <>
              <div className="dag-sidebar__header">
                <span>Tasks</span>
                <span className="dag-sidebar__count">{tasks.length}</span>
              </div>
              <TaskListPanel tasks={tasks} />
            </>
          )}
        </div>
      </div>
    </div>
  );
}
