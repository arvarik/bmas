"use client";

import { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { runProgress, scoreSummary, statusLabel, type BenchmarkRun } from "@/lib/benchmarks";
import { Select } from "@/components/ui/Select";

export function RunsPageClient() {
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [total, setTotal] = useState(0);
  const [status, setStatus] = useState("");
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => { try { const query = status ? `&status=${status}` : ""; const response = await fetch(`/api/benchmarks/runs?limit=200${query}`, { cache: "no-store" }); const data = await response.json() as { runs?: BenchmarkRun[]; total?: number; detail?: string }; if (!response.ok) throw new Error(data.detail ?? "The run catalog request failed"); setRuns(data.runs ?? []); setTotal(data.total ?? 0); setError(null); } catch (reason) { setError(reason instanceof Error ? reason.message : "The run catalog is unavailable"); } }, [status]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  return <div className="benchmarks-page"><header className="page-header"><div><p className="page-eyebrow">Evaluate</p><h2>Runs</h2><p>Track durable benchmark execution, cost, progress, and scorer results.</p></div><ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton></header><section className="benchmark-catalog"><header className="dataset-catalog__toolbar"><div><h3>Benchmark runs</h3><span>{runs.length} of {total} runs</span></div><label className="benchmark-status-filter"><span>Status</span><Select value={status} onChange={(event) => setStatus(event.target.value)}><option value="">All states</option>{["queued", "running", "paused", "completed", "partial", "failed", "cancelled"].map((value) => <option key={value} value={value}>{statusLabel(value)}</option>)}</Select></label></header>{error ? <ResourceState kind="unavailable" title="Run catalog unavailable" description={error} onRetry={load} /> : runs.length === 0 ? <ResourceState kind="empty" title="No benchmark runs" description="Start a run from a published test revision." /> : <div className="benchmark-table-wrap"><table className="benchmark-table"><thead><tr><th>Test</th><th>Status</th><th>Progress</th><th>Score</th><th>Cost</th><th>Created</th></tr></thead><tbody>{runs.map((run) => <tr key={run.id}><td><Link href={`/runs/${encodeURIComponent(run.id)}`}><strong>{run.test_name}</strong><small>Revision {run.revision} · {run.id.slice(-8)}</small></Link></td><td><span className={`benchmark-status benchmark-status--${run.status}`}>{statusLabel(run.status)}</span></td><td>{runProgress(run)}% <small>{run.completed_attempts}/{run.total_attempts}</small></td><td>{scoreSummary(run) === null ? "Pending" : `${(scoreSummary(run) as number * 100).toFixed(1)}%`}</td><td>${Number(run.total_cost_usd ?? 0).toFixed(4)}</td><td>{new Date(run.created_at).toLocaleString()}</td></tr>)}</tbody></table></div>}</section></div>;
}
