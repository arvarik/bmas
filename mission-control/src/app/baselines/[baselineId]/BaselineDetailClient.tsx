"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Eye, Play, RefreshCw } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { FrozenDecisionBar } from "@/components/features/FrozenDecisionBar";
import { ResourceState } from "@/components/ui/ResourceState";
import { useToast } from "@/hooks/useToast";
import {
  formatMetric,
  isFrozenMetric,
  statusLabel,
  type BenchmarkBaseline,
  type BenchmarkGateEvaluation,
  type BenchmarkGatePreview,
  type BenchmarkGateReport,
  type BenchmarkGateRuleResult,
  type BenchmarkRun,
  type RegressionRule,
} from "@/lib/benchmarks";
import { decisionSummary, formatDifference, formatIntervalText, formatProbability, ruleComparison } from "@/lib/frozen-report-presentation";
import { Select } from "@/components/ui/Select";

function metricUnit(metric: string): "percent" | "cost" | "duration" | "tokens" {
  if (isFrozenMetric(metric) || metric.includes("score") || metric.includes("failure")) return "percent";
  if (metric.includes("cost_usd")) return "cost";
  if (metric.includes("duration_ms")) return "duration";
  return "tokens";
}

function ruleMethodLabel(rule: RegressionRule): string {
  const method = rule.analysis_method ?? "point_estimate";
  if (method === "frozen_non_inferiority") return `Frozen non-inferiority · margin ${formatDifference(rule.value)} · ${rule.direction === "reduction" ? "lower is better" : "higher is better"}`;
  if (method === "frozen_superiority") return `Frozen superiority · ${rule.direction === "reduction" ? "lower is better" : "higher is better"}`;
  return statusLabel(method);
}

function FrozenRuleCell({ rule }: { rule: BenchmarkGateRuleResult }) {
  if (!rule.frozen) return <span>{formatMetric(rule.candidate_value, metricUnit(rule.metric))}</span>;
  const comparison = ruleComparison(rule);
  const decision = decisionSummary(rule.frozen.gate, rule.analysis_method === "frozen_superiority" ? "superiority" : "non_inferiority");
  return (
    <div className="benchmark-frozen-rule" data-engine={rule.frozen.engine}>
      {comparison ? <FrozenDecisionBar comparison={comparison} label={rule.label} tone={decision.tone} /> : null}
      <small>
        Estimate {formatDifference(rule.frozen.estimate)} · interval {formatIntervalText(rule.frozen.interval)}
        {rule.frozen.test ? ` · sign-flip p ${formatProbability(rule.frozen.test.p_value)}` : ""}
        {rule.frozen.p_value_adjusted !== undefined ? ` · Holm p ${formatProbability(rule.frozen.p_value_adjusted)}` : ""}
        {rule.frozen.gate?.bound !== undefined && rule.frozen.gate?.bound !== null ? ` · bound ${formatDifference(rule.frozen.gate.bound)}` : ""}
        {rule.frozen.gate?.margin !== undefined && rule.frozen.gate?.margin !== null ? ` · margin ${formatDifference(rule.frozen.gate.margin)}` : ""}
        {rule.frozen.counts ? ` · ${rule.frozen.counts.paired_cases} paired cases` : ""}
        {rule.frozen.reason ? ` · ${rule.frozen.reason}` : ""}
      </small>
      <small>{decision.rule ? `${decision.rule}. ` : ""}{decision.detail}</small>
    </div>
  );
}

function GateReportTable({ report }: { report: BenchmarkGateReport }) {
  return (
    <table className="benchmark-table">
      <thead><tr><th>Rule</th><th>Method</th><th>Baseline</th><th>Candidate or frozen decision</th><th>Boundary</th><th>Result</th></tr></thead>
      <tbody>{report.rules.map((rule) => <tr key={rule.id} data-frozen={rule.frozen ? "true" : "false"}><td>{rule.label}<small><code>{rule.metric}</code></small></td><td>{ruleMethodLabel(rule)}</td><td>{rule.frozen ? "Paired across runs" : formatMetric(rule.baseline_value, metricUnit(rule.metric))}</td><td><FrozenRuleCell rule={rule} /></td><td>{formatMetric(rule.boundary, metricUnit(rule.metric))}</td><td><span className={`benchmark-status benchmark-status--${rule.status}`}>{statusLabel(rule.status)}</span></td></tr>)}</tbody>
    </table>
  );
}

export function BaselineDetailClient({ baselineId }: { baselineId: string }) {
  const { toast } = useToast();
  const [baseline, setBaseline] = useState<BenchmarkBaseline | null>(null);
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [candidateRunId, setCandidateRunId] = useState("");
  const [pending, setPending] = useState(false);
  const [previewing, setPreviewing] = useState(false);
  const [preview, setPreview] = useState<BenchmarkGateReport | null>(null);
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
      // The pinned run stays selectable as a self-check candidate; the
      // daemon accepts it and a passing self-check proves the rule set.
      setRuns((runData.runs ?? []).filter((run) => run.test_id === baselineData.test_id));
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
  const previewGate = async () => {
    setPreviewing(true);
    setError(null);
    try {
      const response = await fetch(`/api/benchmarks/baselines/${encodeURIComponent(baselineId)}/preview`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ candidate_run_id: candidateRunId }) });
      const data = await response.json() as BenchmarkGatePreview & { error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The gate preview failed");
      setPreview(data.report);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The gate preview failed");
    } finally {
      setPreviewing(false);
    }
  };
  const evaluations = useMemo(() => baseline?.evaluations ?? [], [baseline?.evaluations]);
  if (!baseline && !error) return <div className="page-loading">Loading baseline…</div>;
  if (!baseline) return <ResourceState kind="unavailable" title="Baseline unavailable" description={error ?? "The baseline is unavailable."} onRetry={load} />;
  return <div className="benchmarks-page"><header className="page-header"><div><p className="page-eyebrow">Regression baseline</p><h2>{baseline.name}</h2><p>{baseline.test_name} · pinned run <Link href={`/runs/${encodeURIComponent(baseline.run_id)}`}>{baseline.run_id}</Link></p></div><ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton></header>{error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}<section className="benchmark-catalog benchmark-gate-summary"><header className="dataset-catalog__toolbar"><div><h3>Run a regression gate</h3><span>The saved evaluation remains immutable and returns the same result on replay.</span></div></header><div className="benchmark-gate-evaluate"><label>Candidate run<Select value={candidateRunId} onChange={(event) => setCandidateRunId(event.target.value)}><option value="">Select a run from this test</option>{runs.map((run) => <option value={run.id} key={run.id}>{run.id.slice(-8)} · {statusLabel(run.status)} · {new Date(run.created_at).toLocaleString()}{run.id === baseline.run_id ? " · pinned run (self-check)" : ""}</option>)}</Select></label><ActionButton variant="secondary" disabled={!candidateRunId} loading={previewing} onClick={() => void previewGate()}><Eye size={15} /> Preview gate</ActionButton><ActionButton disabled={!candidateRunId} loading={pending} onClick={() => void evaluate()}><Play size={15} /> Evaluate gate</ActionButton></div>{preview ? <section className="benchmark-gate-preview" aria-labelledby="gate-preview-title"><header><h4 id="gate-preview-title">Unsaved preview</h4><span className={`benchmark-status benchmark-status--${preview.status}`}>{statusLabel(preview.status)}</span><small>{preview.reason}{preview.engines?.length ? ` · engines: ${preview.engines.join(", ")}` : ""}</small></header><GateReportTable report={preview} /></section> : null}<div className="benchmark-table-wrap"><table className="benchmark-table"><caption>Immutable regression rules</caption><thead><tr><th>Rule</th><th>Metric</th><th>Method</th><th>Operator</th><th>Limit</th><th>Frozen options</th></tr></thead><tbody>{baseline.rules.map((rule) => <tr key={rule.id} data-method={rule.analysis_method ?? "point_estimate"}><td>{rule.label}</td><td><code>{rule.metric}</code></td><td>{ruleMethodLabel(rule)}</td><td>{statusLabel(rule.operator)}</td><td>{rule.value}</td><td>{isFrozenMetric(rule.metric) ? `${rule.resample_count ?? 999} resamples · at least ${rule.minimum_usable_cases ?? 1} usable cases` : "Legacy report engine"}</td></tr>)}</tbody></table></div></section><section className="benchmark-catalog"><header className="dataset-catalog__toolbar"><div><h3>Gate history</h3><span>{evaluations.length} saved evaluations</span></div></header>{evaluations.length === 0 ? <ResourceState kind="empty" title="No gate evaluations" description="Select a candidate run to record the first result." /> : <div className="benchmark-evaluations">{evaluations.map((evaluation) => <article key={evaluation.id}><header><span className={`benchmark-status benchmark-status--${evaluation.status}`}>{statusLabel(evaluation.status)}</span><Link href={`/runs/${encodeURIComponent(evaluation.candidate_run_id)}`}>{evaluation.candidate_run_id}</Link><code>{evaluation.report_checksum.slice(0, 12)}</code></header><p>{evaluation.report.reason}{evaluation.report.engines?.length ? <small> Engines: {evaluation.report.engines.join(", ")}.</small> : null}</p><GateReportTable report={evaluation.report} /></article>)}</div>}</section></div>;
}
