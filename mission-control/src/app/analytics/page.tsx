"use client";

import { useEffect, useState } from "react";
import { ActionableError } from "@/components/ui/ActionableError";

interface TaskAnalytics {
  task_count: number;
  total_cost_usd: number;
  total_tokens: number;
  average_duration_ms: number;
  archived_count: number;
  by_status: Record<string, number>;
}

export default function AnalyticsPage() {
  const [data, setData] = useState<TaskAnalytics | null>(null);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let cancelled = false;
    void fetch("/api/tasks/analytics", { cache: "no-store" })
      .then(async (response) => {
        const body = await response.json();
        if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
        return body as TaskAnalytics;
      })
      .then((body) => { if (!cancelled) { setData(body); setError(""); } })
      .catch((reason: unknown) => { if (!cancelled) setError(reason instanceof Error ? reason.message : "Analytics are unavailable."); });
    return () => { cancelled = true; };
  }, [version]);

  return (
    <div className="analytics-page">
      <header className="page-header"><div><p className="page-eyebrow">Analyze</p><h2>Analytics</h2><p>These figures use the complete active task set.</p></div></header>
      {error ? <ActionableError component="Task analytics" cause={error} onRetry={() => setVersion((value) => value + 1)} /> : null}
      {!data && !error ? <p role="status">Loading analytics…</p> : null}
      {data ? <>
        <section className="analytics-metrics" aria-label="Task totals">
          <article><span>Active tasks</span><strong>{data.task_count.toLocaleString()}</strong></article>
          <article><span>Total cost</span><strong>${data.total_cost_usd.toFixed(4)}</strong></article>
          <article><span>Total tokens</span><strong>{data.total_tokens.toLocaleString()}</strong></article>
          <article><span>Average duration</span><strong>{Math.round(data.average_duration_ms / 1000).toLocaleString()}s</strong></article>
          <article><span>Archived tasks</span><strong>{data.archived_count.toLocaleString()}</strong></article>
        </section>
        <section className="analytics-status"><h3>Tasks by status</h3><ul>{Object.entries(data.by_status).map(([status, count]) => <li key={status}><span>{status}</span><strong>{count}</strong></li>)}</ul></section>
      </> : null}
    </div>
  );
}
