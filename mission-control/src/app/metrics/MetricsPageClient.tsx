"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { Plus, RefreshCw, X } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { MetricDefinitionFields, type ScorerOption } from "@/components/features/MetricDefinitionFields";
import { ResourceState } from "@/components/ui/ResourceState";
import { useToast } from "@/hooks/useToast";
import { statusLabel } from "@/lib/benchmarks";
import {
  buildMetricDefinition,
  defaultMetricForm,
  lifecycleLabel,
  metricFormErrors,
  type MetricDefinitionForm,
  type StoredMetricDefinition,
} from "@/lib/metric-lifecycle-presentation";

export function MetricsPageClient() {
  const { toast } = useToast();
  const [metrics, setMetrics] = useState<StoredMetricDefinition[] | null>(null);
  const [scorers, setScorers] = useState<ScorerOption[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [authoring, setAuthoring] = useState(false);
  const [pending, setPending] = useState(false);
  const [form, setForm] = useState<MetricDefinitionForm>(() => defaultMetricForm());
  const load = useCallback(async () => {
    try {
      const [metricResponse, scorerResponse] = await Promise.all([
        fetch("/api/evaluation/metrics", { cache: "no-store" }),
        fetch("/api/benchmarks/scorers", { cache: "no-store" }),
      ]);
      const metricData = await metricResponse.json() as { metrics?: StoredMetricDefinition[]; error?: string; detail?: string };
      if (!metricResponse.ok) throw new Error(metricData.error ?? metricData.detail ?? "The metric definitions are unavailable");
      setMetrics(metricData.metrics ?? []);
      if (scorerResponse.ok) {
        const scorerData = await scorerResponse.json() as { scorers?: ScorerOption[] };
        setScorers(scorerData.scorers ?? []);
      }
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The metric definitions are unavailable");
    }
  }, []);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  const errors = useMemo(() => metricFormErrors(form), [form]);
  const update = (patch: Partial<MetricDefinitionForm>) => setForm((current) => ({ ...current, ...patch }));
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (errors.length) return;
    setPending(true);
    setError(null);
    try {
      const response = await fetch("/api/evaluation/metrics", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ record: buildMetricDefinition(form) }),
      });
      const data = await response.json() as { id?: string; error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The metric definition could not be registered");
      toast({ type: "success", message: `Metric ${form.metric_id} registered as a draft. Validate and publish it from its detail page.` });
      setForm(defaultMetricForm());
      setAuthoring(false);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The metric definition could not be registered");
    } finally {
      setPending(false);
    }
  };
  const counts = useMemo(() => {
    const tally: Record<string, number> = {};
    for (const metric of metrics ?? []) tally[metric.lifecycle_state] = (tally[metric.lifecycle_state] ?? 0) + 1;
    return tally;
  }, [metrics]);

  return (
    <div className="benchmarks-page">
      <header className="page-header">
        <div><p className="page-eyebrow">Evaluate</p><h2>Metric definitions</h2><p>Every displayed metric references one published definition. Register a definition, validate it, and publish it before a frozen report can serve.</p></div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
          <ActionButton variant={authoring ? "secondary" : "primary"} onClick={() => setAuthoring((value) => !value)}>{authoring ? <X size={15} /> : <Plus size={15} />} {authoring ? "Close" : "Register definition"}</ActionButton>
        </div>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      {authoring ? (
        <form className="benchmark-form metric-form" onSubmit={submit} aria-labelledby="metric-form-title">
          <header><div><h3 id="metric-form-title">Register a metric definition</h3><p>The definition starts as a draft. Publication needs the complete contract and a current calibration.</p></div></header>
          <MetricDefinitionFields form={form} update={update} scorers={scorers} />
          {errors.length ? <ul className="benchmark-report__warnings metric-form__errors" aria-label="Definition problems">{errors.map((entry) => <li key={entry}>{entry}</li>)}</ul> : null}
          <div className="benchmark-form__actions"><ActionButton type="submit" loading={pending} disabled={errors.length > 0}>Register draft definition</ActionButton></div>
        </form>
      ) : null}
      <section className="benchmark-catalog" aria-labelledby="metric-catalog-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="metric-catalog-title">Registered definitions</h3><span>{metrics ? `${metrics.length} definitions` : "Loading"}{counts.published ? ` · ${counts.published} published` : ""}{counts.draft ? ` · ${counts.draft} draft` : ""}</span></div></header>
        {metrics && metrics.length === 0 && !error ? <ResourceState kind="empty" title="No metric definitions" description="Register a definition so frozen reports can resolve their metrics." /> : null}
        {metrics && metrics.length ? (
          <div className="benchmark-table-wrap">
            <table className="benchmark-table">
              <caption>Definitions by lifecycle state</caption>
              <thead><tr><th>Metric</th><th>Lifecycle</th><th>Calibration</th><th>Scorer</th><th>Direction</th><th>Registered</th></tr></thead>
              <tbody>{metrics.map((metric) => <tr key={metric.metric_id} data-state={metric.lifecycle_state}><td><Link href={`/metrics/${encodeURIComponent(metric.metric_id)}`}>{metric.metric_id}</Link></td><td><span className={`benchmark-status benchmark-status--${metric.lifecycle_state === "published" ? "passed" : metric.lifecycle_state === "draft" ? "queued" : metric.lifecycle_state === "validated" ? "provisional" : "cancelled"}`}>{lifecycleLabel(metric.lifecycle_state)}</span></td><td>{statusLabel(metric.calibration_state)}</td><td>{metric.record.scorer.scorer_id} <small>v{metric.record.scorer.version}</small></td><td>{statusLabel(metric.record.measurement.direction)}</td><td>{new Date(metric.created_at).toLocaleString()}</td></tr>)}</tbody>
            </table>
          </div>
        ) : null}
      </section>
    </div>
  );
}
