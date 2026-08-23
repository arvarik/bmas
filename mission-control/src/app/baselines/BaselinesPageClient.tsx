"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Plus, RefreshCw, Trash2, X } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { ResourceState } from "@/components/ui/ResourceState";
import { useToast } from "@/hooks/useToast";
import {
  reportMetricOptions,
  statusLabel,
  supportedAnalysisMethods,
  type BenchmarkBaseline,
  type BenchmarkRun,
  type BenchmarkRunReport,
  type RegressionOperator,
  type RegressionRule,
} from "@/lib/benchmarks";
import { Select } from "@/components/ui/Select";

const newRule = (id = "rule-1"): RegressionRule => ({
  id,
  label: "",
  metric: "",
  operator: "max_drop",
  value: 0,
  analysis_method: "point_estimate",
});

export function BaselinesPageClient() {
  const { toast } = useToast();
  const [baselines, setBaselines] = useState<BenchmarkBaseline[]>([]);
  const [runs, setRuns] = useState<BenchmarkRun[]>([]);
  const [selectedRunId, setSelectedRunId] = useState("");
  const [report, setReport] = useState<BenchmarkRunReport | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [rules, setRules] = useState<RegressionRule[]>([newRule()]);
  const [authoring, setAuthoring] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    try {
      const [baselineResponse, runResponse] = await Promise.all([
        fetch("/api/benchmarks/baselines", { cache: "no-store" }),
        fetch("/api/benchmarks/runs?status=completed&limit=200", { cache: "no-store" }),
      ]);
      const baselineData = await baselineResponse.json() as { baselines?: BenchmarkBaseline[]; error?: string; detail?: string };
      const runData = await runResponse.json() as { runs?: BenchmarkRun[]; error?: string; detail?: string };
      if (!baselineResponse.ok) throw new Error(baselineData.error ?? baselineData.detail ?? "The baseline catalog is unavailable");
      if (!runResponse.ok) throw new Error(runData.error ?? runData.detail ?? "The completed run catalog is unavailable");
      setBaselines(baselineData.baselines ?? []);
      setRuns(runData.runs ?? []);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The baseline catalog is unavailable");
    }
  }, []);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  useEffect(() => {
    if (!selectedRunId) return;
    const controller = new AbortController();
    void fetch(`/api/benchmarks/runs/${encodeURIComponent(selectedRunId)}/report`, {
      cache: "no-store",
      signal: controller.signal,
    })
      .then(async (response) => {
        const data = await response.json() as BenchmarkRunReport & { error?: string; detail?: string };
        if (!response.ok) throw new Error(data.error ?? data.detail ?? "The run metrics are unavailable");
        setReport(data);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : "The run metrics are unavailable");
      });
    return () => controller.abort();
  }, [selectedRunId]);
  const metricOptions = useMemo(() => report ? reportMetricOptions(report) : [], [report]);
  const updateRule = (index: number, patch: Partial<RegressionRule>) => {
    setRules((current) => current.map((rule, ruleIndex) => ruleIndex === index ? { ...rule, ...patch } : rule));
  };
  const selectMetric = (index: number, metric: string) => {
    const currentMethod = rules[index]?.analysis_method ?? "point_estimate";
    updateRule(index, {
      metric,
      analysis_method: supportedAnalysisMethods(metric).includes(currentMethod)
        ? currentMethod
        : "point_estimate",
    });
  };
  const selectRun = (runId: string) => {
    setSelectedRunId(runId);
    setReport(null);
  };
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    setPending(true);
    setError(null);
    try {
      const response = await fetch("/api/benchmarks/baselines", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_id: selectedRunId, name, description, rules }),
      });
      const data = await response.json() as BenchmarkBaseline & { error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The baseline could not be pinned");
      toast({ type: "success", message: "Baseline pinned. The run and rule set are now immutable." });
      setName("");
      setDescription("");
      setSelectedRunId("");
      setRules([newRule()]);
      setAuthoring(false);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The baseline could not be pinned");
    } finally {
      setPending(false);
    }
  };
  const valid = Boolean(selectedRunId && name.trim() && rules.length && rules.every((rule) => rule.label.trim() && rule.metric));

  return <div className="benchmarks-page">
    <header className="page-header"><div><p className="page-eyebrow">Evaluate</p><h2>Baselines and gates</h2><p>Pin trusted runs and apply repeatable release checks to later runs.</p></div><div className="page-header__actions"><ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton><ActionButton variant={authoring ? "secondary" : "primary"} onClick={() => setAuthoring((value) => !value)}>{authoring ? <X size={15} /> : <Plus size={15} />} {authoring ? "Close" : "Pin baseline"}</ActionButton></div></header>
    {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
    {authoring ? <form className="benchmark-form benchmark-baseline-form" onSubmit={submit}><header><div><h3>Pin a completed run</h3><p>A baseline cannot change or be deleted after creation.</p></div></header><div className="benchmark-form__grid"><label>Completed run<Select required value={selectedRunId} onChange={(event) => selectRun(event.target.value)}><option value="">Select a run</option>{runs.map((run) => <option key={run.id} value={run.id}>{run.test_name} revision {run.revision} · {run.id.slice(-8)}</option>)}</Select></label><label>Baseline name<input required value={name} onChange={(event) => setName(event.target.value)} placeholder="Release candidate standard" /></label><label className="benchmark-form__wide">Description<textarea rows={2} value={description} onChange={(event) => setDescription(event.target.value)} placeholder="State when and why this baseline applies." /></label></div><fieldset className="benchmark-form__section"><legend>Regression rules</legend>{rules.map((rule, index) => { const methods = supportedAnalysisMethods(rule.metric); return <div className="benchmark-gate-rule" key={rule.id}><label>Label<input required value={rule.label} onChange={(event) => updateRule(index, { label: event.target.value })} placeholder="Accuracy does not regress" /></label><label>Metric<Select required value={rule.metric} onChange={(event) => selectMetric(index, event.target.value)}><option value="">Select a metric</option>{metricOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}</Select></label><label>Measure<Select value={rule.analysis_method ?? "point_estimate"} onChange={(event) => updateRule(index, { analysis_method: event.target.value as RegressionRule["analysis_method"] })}><option value="point_estimate">Point estimate</option><option value="lower_confidence_bound" disabled={!methods.includes("lower_confidence_bound")}>Lower 95% bound</option><option value="upper_confidence_bound" disabled={!methods.includes("upper_confidence_bound")}>Upper 95% bound</option><option value="holm_sign_test" disabled={!methods.includes("holm_sign_test")}>Holm-adjusted sign test</option></Select></label><label>Rule<Select value={rule.operator} onChange={(event) => updateRule(index, { operator: event.target.value as RegressionOperator })}><option value="max_drop">Maximum drop</option><option value="max_increase_ratio">Maximum increase ratio</option><option value="gte">Minimum value</option><option value="lte">Maximum value</option></Select></label><label>Limit<input required type="number" step="any" value={rule.value} onChange={(event) => updateRule(index, { value: Number(event.target.value) })} /></label><button type="button" className="benchmark-icon-button" disabled={rules.length === 1} aria-label={`Remove rule ${index + 1}`} onClick={() => setRules((current) => current.filter((_, ruleIndex) => ruleIndex !== index))}><Trash2 size={15} /></button></div>; })}<ActionButton type="button" variant="secondary" onClick={() => setRules((current) => [...current, newRule(`rule-${crypto.randomUUID()}`)])}><Plus size={15} /> Add rule</ActionButton></fieldset><p className="benchmark-baseline-warning">Review every rule. Creation locks the selected run, statistical method, thresholds, and checksum.</p><div className="benchmark-form__actions"><ActionButton loading={pending} disabled={!valid} type="submit">Pin immutable baseline</ActionButton></div></form> : null}
    <section className="benchmark-catalog" aria-labelledby="baseline-catalog-title"><header className="dataset-catalog__toolbar"><div><h3 id="baseline-catalog-title">Pinned baselines</h3><span>{baselines.length} immutable records</span></div></header>{!error && baselines.length === 0 ? <ResourceState kind="empty" title="No baselines" description="Pin a completed benchmark run to define release gates." /> : <ul className="benchmark-card-grid">{baselines.map((baseline) => <li key={baseline.id}><Link href={`/baselines/${encodeURIComponent(baseline.id)}`} className="benchmark-card"><header><div><strong>{baseline.name}</strong><small>{baseline.test_name}</small></div><span className={`benchmark-status benchmark-status--${baseline.latest_gate_status ?? "queued"}`}>{baseline.latest_gate_status ? statusLabel(baseline.latest_gate_status) : "Not evaluated"}</span></header><p>{baseline.description || "No description was provided."}</p><dl><div><dt>Rules</dt><dd>{baseline.rules.length}</dd></div><div><dt>Evaluations</dt><dd>{baseline.evaluation_count ?? 0}</dd></div><div><dt>Run</dt><dd>{baseline.run_id.slice(-8)}</dd></div><div><dt>Created</dt><dd>{new Date(baseline.created_at).toLocaleDateString()}</dd></div></dl></Link></li>)}</ul>}</section>
  </div>;
}
