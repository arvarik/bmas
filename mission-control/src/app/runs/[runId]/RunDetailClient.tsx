"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { BackLink } from "@/components/ui/BackLink";
import Link from "next/link";
import { Ban, Pause, Play, RefreshCw, RotateCcw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { AnalysisHistoryPanel } from "@/components/features/AnalysisHistoryPanel";
import { AttemptEvidencePanel } from "@/components/features/AttemptEvidencePanel";
import { BenchmarkRunReportPanel } from "@/components/features/BenchmarkRunReportPanel";
import { BenchmarkHumanReviewForm } from "@/components/features/BenchmarkHumanReviewForm";
import { ReplayBundlePanel } from "@/components/features/ReplayBundlePanel";
import { ResourceLedgerPanel } from "@/components/features/ResourceLedgerPanel";
import { RunStudyPanel } from "@/components/features/RunStudyPanel";
import { ResourceState } from "@/components/ui/ResourceState";
import { costBadge, primaryMetric, runProgress, scoringBadge, statusLabel, type BenchmarkRun, type BenchmarkScore } from "@/lib/benchmarks";

export function RunDetailClient({ runId }: { runId: string }) {
  const [run, setRun] = useState<BenchmarkRun | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`/api/benchmarks/runs/${encodeURIComponent(runId)}`, { cache: "no-store" });
      const data = await response.json() as BenchmarkRun & { detail?: string };
      if (!response.ok) throw new Error(data.detail ?? "The run is unavailable");
      setRun(data);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The run is unavailable");
    }
  }, [runId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  useEffect(() => {
    if (!run || !["queued", "running", "cancelling"].includes(run.status)) return;
    const interval = window.setInterval(() => void load(), 3000);
    return () => window.clearInterval(interval);
  }, [load, run]);
  const scores = useMemo(() => {
    const grouped = new Map<string, BenchmarkScore[]>();
    for (const score of run?.scores ?? []) {
      grouped.set(score.attempt_id, [...(grouped.get(score.attempt_id) ?? []), score]);
    }
    return grouped;
  }, [run?.scores]);
  const action = async (name: string) => {
    setPending(name);
    setError(null);
    try {
      const response = await fetch(`/api/benchmarks/runs/${encodeURIComponent(runId)}/${name}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      const data = await response.json() as { detail?: string };
      if (!response.ok) throw new Error(data.detail ?? "The run action failed");
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The run action failed");
    } finally {
      setPending(null);
    }
  };
  if (!run && !error) return <div className="page-loading">Loading run…</div>;
  if (!run) return <ResourceState kind="unavailable" title="Run unavailable" description={error ?? "The run is unavailable."} onRetry={load} />;
  const primary = primaryMetric(run);
  const badge = scoringBadge(run);
  const cost = costBadge(run);
  return (
    <div className="benchmarks-page">
      <BackLink href="/runs" label="Runs" />
      <header className="page-header benchmark-run-header">
        <div><p className="page-eyebrow">Benchmark run</p><h2>{run.test_name}</h2><p>Revision {run.revision} · {run.id}</p></div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
          {run.status === "running" || run.status === "queued" ? <ActionButton variant="secondary" loading={pending === "pause"} onClick={() => void action("pause")}><Pause size={15} /> Pause admission</ActionButton> : null}
          {run.status === "paused" ? <ActionButton loading={pending === "resume"} onClick={() => void action("resume")}><Play size={15} /> Resume</ActionButton> : null}
          {["failed", "partial", "cancelled"].includes(run.status) ? <ActionButton variant="secondary" loading={pending === "retry"} onClick={() => void action("retry")}><RotateCcw size={15} /> Retry failed</ActionButton> : null}
          {["queued", "running", "paused"].includes(run.status) ? <ActionButton variant="danger" loading={pending === "cancel"} onClick={() => void action("cancel")}><Ban size={15} /> Cancel</ActionButton> : null}
        </div>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      <section className="benchmark-run-overview" aria-label="Run summary">
        <div><span>Status</span><strong className={`benchmark-status benchmark-status--${run.status}`}>{statusLabel(run.status)}</strong>{badge ? <small className="benchmark-status benchmark-status--failed">{badge}</small> : null}</div>
        <div><span>Progress</span><strong>{runProgress(run)}%</strong><small>{run.completed_attempts} of {run.total_attempts} attempts · {run.aggregates?.failed_attempts ?? 0} failed</small></div>
        <div><span>{primary ? primary.scorer_name : "Primary metric"}</span><strong>{primary?.mean == null ? "Pending" : `${(primary.mean * 100).toFixed(1)}%`}</strong><small>{primary ? `${primary.count} scored` : "No primary scorer"}</small></div>
        <div><span>Cost</span><strong>${Number(run.aggregates?.total_cost_usd ?? run.total_cost_usd ?? 0).toFixed(4)}</strong>{cost ? <small>{cost}</small> : null}</div>
        {(run.aggregates?.secondary_metrics ?? []).map((metric) => <div key={metric.scorer_id}><span>{metric.scorer_name}</span><strong>{metric.mean == null ? "Pending" : `${(metric.mean * 100).toFixed(1)}%`}</strong><small>{metric.count} scored</small></div>)}
      </section>
      <BenchmarkRunReportPanel run={run} />
      <RunStudyPanel runId={run.id} blockedAttempts={(run.attempts ?? []).filter((attempt) => attempt.failure_category === "configuration" && (attempt.error_message ?? "").includes("study conditions")).length} />
      <AnalysisHistoryPanel runId={run.id} run={run} />
      <ResourceLedgerPanel runId={run.id} />
      <ReplayBundlePanel runId={run.id} />
      <section className="benchmark-catalog">
        <header className="dataset-catalog__toolbar"><div><h3>Attempts</h3><span>Retries remain visible for complete provenance.</span></div></header>
        <div className="benchmark-table-wrap"><table className="benchmark-table">
          <thead><tr><th>Arm and item</th><th>Repeat</th><th>Status</th><th>Scores</th><th>Runtime</th><th>Task</th></tr></thead>
          <tbody>{run.attempts?.map((attempt) => {
            const attemptScores = scores.get(attempt.id) ?? [];
            return <tr key={attempt.id}>
              <td><strong>{attempt.arm_name}</strong><small>{attempt.item_key}</small></td>
              <td>{attempt.repeat_index}{attempt.retry_index ? <small>Retry {attempt.retry_index}</small> : null}</td>
              <td><span className={`benchmark-status benchmark-status--${attempt.status}`}>{statusLabel(attempt.status)}</span>{attempt.failure_category ? <small>{statusLabel(attempt.failure_category)} failure</small> : null}{attempt.error_message ? <small>{attempt.error_message}</small> : null}{attempt.status === "completed" ? <details><summary>Human review</summary><BenchmarkHumanReviewForm attemptId={attempt.id} existing={run.human_reviews?.find((review) => review.attempt_id === attempt.id)} onSaved={load} /></details> : null}</td>
              <td>{attemptScores.length === 0 ? "Pending" : attemptScores.map((score) => <div className="benchmark-score" key={score.id}><strong>{score.scorer_name} v{score.scorer_version}</strong><span>{score.score === null ? statusLabel(score.status) : `${Number(score.score) * 100}%`}</span>{score.explanation ? <details><summary>Evidence</summary><p>{score.explanation}</p><pre>{JSON.stringify(score.evidence, null, 2)}</pre></details> : null}</div>)}<AttemptEvidencePanel attemptId={attempt.id} /></td>
              <td>{attempt.duration_ms ? `${(attempt.duration_ms / 1000).toFixed(1)}s` : "Pending"}<small>{attempt.total_tokens?.toLocaleString() ?? 0} tokens · ${Number(attempt.total_cost_usd ?? 0).toFixed(4)}</small></td>
              <td>{attempt.task_id ? <Link href={`/task/${encodeURIComponent(attempt.task_id)}`}>Open task</Link> : "Not admitted"}</td>
            </tr>;
          })}</tbody>
        </table></div>
      </section>
    </div>
  );
}
