import { Suspense } from "react";
import { TasksPageClient } from "./TasksPageClient";

export default function TasksPage() {
  return (
    <Suspense fallback={<div className="page-loading">Loading tasks…</div>}>
      <TasksPageClient />
    </Suspense>
  );
}
