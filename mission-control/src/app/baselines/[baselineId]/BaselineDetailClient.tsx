"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Play, RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { useToast } from "@/hooks/useToast";
import { formatMetric, statusLabel, type BenchmarkBaseline, type BenchmarkGateEvaluation, type BenchmarkRun } from "@/lib/benchmarks";

function metricUnit(metric: string): "percent" | "cost" | "duration" | "tokens" {
  if (metric.includes("score") || metric.includes("failure")) return "percent";
  if (metric.includes("cost_usd")) return "cost";
  if (metric.includes("duration_ms")) return "duration";
  return "tokens";
}

export function BaselineDetailClient({ baselineId }: { baselineId: string }) {
  const { toast } = useToast();
  const [baseline, setBaseline] = useState<BenchmarkBaseline | null>(null);
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [candidateRunId, setCandidateRunId] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const [baselineResponse, runResponse] = await Promise.all([
        fetch(`/api/benchmarks/baselines/${encodeURIComponent(baselineId)}`, { cache: "no-store" }),
        fetch("/api/benchmarks/runs?limit=200", { cache: "no-store" }),
      ]);
      const baselineData = await baselineResponse.json() as BenchmarkBaseline & { error?: string; detail?: string };
      const runData = await runResponse.json() as { runs?: BenchmarkRun[]; error?: string; detail?: string };
      if (!baselineResponse.ok) throw new Error(baselineData.error ?? baselineData.detail ?? "The baseline is unavailable");
      if (!runResponse.ok) throw new Error(runData.error ?? runData.detail ?? "The run catalog is unavailable");
      setBaseline(baselineData);
      setRuns((runData.runs ?? []).filter((run) => run.test_id === baselineData.test_id && run.id !== baselineData.run_id));
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The baseline is unavailable");
    }
  }, [baselineId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const evaluate = async () => {
    setPending(true);
    setError(null);
    try {
      const response = await fetch(`/api/benchmarks/baselines/${encodeURIComponent(baselineId)}/evaluate`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_run_id: candidateRunId }) });
      const data = await response.json() as BenchmarkGateEvaluation & { error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The gate evaluation failed");
      toast({ type: data.status === "failed" ? "error" : "success", message: `Gate ${data.status}. ${data.report.reason}` });
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The gate evaluation failed");
    } finally {
      setPending(false);
    }
  };
  const evaluations = useMemo(() => baseline?.evaluations ?? [], [baseline?.evaluations]);
  if (!baseline && !error) return <div className="page-loading">Loading baseline…</div>;
  if (!baseline) return <ResourceState kind="unavailable" title="Baseline unavailable" description={error ?? "The baseline is unavailable."} onRetry={load} />;
  return <div className="benchmarks-page"><header className="page-header"><div><p className="page-eyebrow">Regression baseline</p><h2>{baseline.name}</h2><p>{baseline.test_name} · pinned run <Link href={`/runs/${encodeURIComponent(baseline.run_id)}`}>{baseline.run_id}</Link></p></div><ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton></header>{error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}<section className="benchmark-catalog benchmark-gate-summary"><header className="dataset-catalog__toolbar"><div><h3>Run a regression gate</h3><span>The saved evaluation remains immutable and returns the same result on replay.</span></div></header><div className="benchmark-gate-evaluate"><label>Candidate run<select value={candidateRunId} onChange={(event) => setCandidateRunId(event.target.value)}><option value="">Select a run from this test</option>{runs.map((run) => <option value={run.id} key={run.id}>{run.id.slice(-8)} · {statusLabel(run.status)} · {new Date(run.created_at).toLocaleString()}</option>)}</select></label><ActionButton disabled={!candidateRunId} loading={pending} onClick={() => void evaluate()}><Play size={15} /> Evaluate gate</ActionButton></div><div className="benchmark-table-wrap"><table className="benchmark-table"><caption>Immutable regression rules</caption><thead><tr><th>Rule</th><th>Metric</th><th>Operator</th><th>Limit</th></tr></thead><tbody>{baseline.rules.map((rule) => <tr key={rule.id}><td>{rule.label}</td><td><code>{rule.metric}</code></td><td>{statusLabel(rule.operator)}</td><td>{rule.value}</td></tr>)}</tbody></table></div></section><section className="benchmark-catalog"><header className="dataset-catalog__toolbar"><div><h3>Gate history</h3><span>{evaluations.length} saved evaluations</span></div></header>{evaluations.length === 0 ? <ResourceState kind="empty" title="No gate evaluations" description="Select a candidate run to record the first result." /> : <div className="benchmark-evaluations">{evaluations.map((evaluation) => <article key={evaluation.id}><header><span className={`benchmark-status benchmark-status--${evaluation.status}`}>{statusLabel(evaluation.status)}</span><Link href={`/runs/${encodeURIComponent(evaluation.candidate_run_id)}`}>{evaluation.candidate_run_id}</Link><code>{evaluation.report_checksum.slice(0, 12)}</code></header><p>{evaluation.report.reason}</p><table className="benchmark-table"><thead><tr><th>Rule</th><th>Baseline</th><th>Candidate</th><th>Boundary</th><th>Result</th></tr></thead><tbody>{evaluation.report.rules.map((rule) => <tr key={rule.id}><td>{rule.label}</td><td>{formatMetric(rule.baseline_value, metricUnit(rule.metric))}</td><td>{formatMetric(rule.candidate_value, metricUnit(rule.metric))}</td><td>{formatMetric(rule.boundary, metricUnit(rule.metric))}</td><td><span className={`benchmark-status benchmark-status--${rule.status}`}>{statusLabel(rule.status)}</span></td></tr>)}</tbody></table></article>)}</div>}</section></div>;
}
