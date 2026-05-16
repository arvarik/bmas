"use client";

/**
 * DAG Tab — /task/[taskId]/dag
 *
 * Renders the DAG visualization for a specific task. Consumes sub-task
 * data from TaskStreamContext and wraps it into the Task[] shape that
 * DAGVisualizer expects.
 */

import dynamic from "next/dynamic";
import { useTaskData } from "../TaskStreamContext";
import { Skeleton } from "@/components/ui/Skeleton";
import type { Task } from "@/hooks/useTaskStream";

const DAGVisualizer = dynamic(() => import("@/components/features/DAGVisualizer"), {
  ssr: false,
  loading: () => (
    <div style={{
      height: "100%", display: "flex", alignItems: "center",
      justifyContent: "center", background: "var(--surface-overlay)",
      borderRadius: "var(--radius-lg)",
    }}>
      <Skeleton variant="dag" />
    </div>
  ),
});

export default function DAGPage() {
  const { subTasks, isLive, taskMeta } = useTaskData();

  // Build Task[] shape expected by DAGVisualizer
  const tasks: Task[] = taskMeta
    ? [{
        id: taskMeta.task_id,
        label: taskMeta.label,
        status: taskMeta.status,
        sub_tasks: subTasks,
        created_at: taskMeta.created_at,
        updated_at: taskMeta.created_at,
      }]
    : [];

  return (
    <div className="view-container dag-view" style={{ overflow: "hidden" }}>
      <div className="dag-layout">
        <div className="dag-canvas">
          <DAGVisualizer
            tasks={tasks}
            loading={!taskMeta && isLive}
          />
        </div>
      </div>
    </div>
  );
}
