"use client";

import Link from "next/link";
import { useTaskHistory } from "@/hooks/useTaskHistory";
import { ActionableError } from "@/components/ui/ActionableError";

export default function ActivityPage() {
  const running = useTaskHistory({ status: "running", sort: "activity-desc" });
  const attention = useTaskHistory({ status: "attention", sort: "activity-desc" });

  return (
    <div className="activity-page">
      <header className="page-header">
        <div><p className="page-eyebrow">Observe</p><h2>Live activity</h2><p>See active work and tasks that need an operator response.</p></div>
      </header>
      {running.error || attention.error ? <ActionableError component="Live activity" cause={running.error || attention.error || "Unknown error"} onRetry={() => { void running.refetch(); void attention.refetch(); }} /> : null}
      <div className="activity-grid">
        <section><header><h3>Running</h3><span>{running.total}</span></header>{running.tasks.length ? <ul>{running.tasks.map((task) => <li key={task.id}><Link href={`/task/${task.id}`}><strong>{task.label}</strong><span>{task.run_state || "running"}</span></Link></li>)}</ul> : <p>No tasks run now.</p>}</section>
        <section><header><h3>Needs attention</h3><span>{attention.total}</span></header>{attention.tasks.length ? <ul>{attention.tasks.map((task) => <li key={task.id}><Link href={`/task/${task.id}`}><strong>{task.label}</strong><span>{task.pending_approval ? "Approval required" : task.stale ? "Stale" : task.error_message || task.run_state || task.status}</span></Link></li>)}</ul> : <p>No tasks need attention.</p>}</section>
      </div>
    </div>
  );
}
