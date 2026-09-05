"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { ArrowRight, Pencil, RefreshCw, X } from "lucide-react";
import { MetricDefinitionFields, type ScorerOption } from "@/components/features/MetricDefinitionFields";
import { ActionButton } from "@/components/ui/ActionButton";
import { BackLink } from "@/components/ui/BackLink";
import { ResourceState } from "@/components/ui/ResourceState";
import { useToast } from "@/hooks/useToast";
import { statusLabel } from "@/lib/benchmarks";
import {
  advanceRequest,
  buildMetricDefinition,
  calibrationSummary,
  formFromRecord,
  lifecycleLabel,
  lifecycleSteps,
  metricFormErrors,
  nextTransitions,
  type LifecycleState,
  type MetricDefinitionForm,
  type StoredMetricDefinition,
} from "@/lib/metric-lifecycle-presentation";

const TRANSITION_LABELS: Record<LifecycleState, string> = {
  draft: "Return to draft",
  validated: "Validate",
  published: "Publish",
  deprecated: "Deprecate",
  withdrawn: "Withdraw",
};

function isoNow(): string {
  return new Date().toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function MetricDetailClient({ metricId }: { metricId: string }) {
  const { toast } = useToast();
  const [stored, setStored] = useState<StoredMetricDefinition | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<LifecycleState | null>(null);
  const [evidence, setEvidence] = useState({ schema: true, fixture: true, evidence: true });
  const [reason, setReason] = useState("");
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState<MetricDefinitionForm | null>(null);
  const [scorers, setScorers] = useState<ScorerOption[]>([]);
  const [revising, setRevising] = useState(false);
  const load = useCallback(async () => {
    try {
      const response = await fetch(`/api/evaluation/metrics/${encodeURIComponent(metricId)}`, { cache: "no-store" });
      const data = await response.json() as StoredMetricDefinition & { id?: string; error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The metric definition is unavailable");
      setStored({ ...data, metric_id: data.metric_id ?? data.id ?? metricId, lifecycle_state: (data.lifecycle_state ?? data.record.lifecycle_state) as LifecycleState });
      setError(null);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The metric definition is unavailable");
    }
  }, [metricId]);
  useEffect(() => { void Promise.resolve().then(load); }, [load]);
  useEffect(() => {
    let active = true;
    void fetch("/api/benchmarks/scorers", { cache: "no-store" }).then(async (response) => {
      if (!active || !response.ok) return;
      const data = await response.json() as { scorers?: ScorerOption[] };
      if (active) setScorers(data.scorers ?? []);
    }).catch(() => undefined);
    return () => { active = false; };
  }, []);
  const state = stored?.lifecycle_state ?? "draft";
  const formErrors = useMemo(() => (form ? metricFormErrors(form) : []), [form]);
  const startEditing = () => {
    if (!stored) return;
    setForm(formFromRecord(stored.record));
    setEditing(true);
  };
  const revise = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!form || formErrors.length) return;
    setRevising(true);
    setError(null);
    try {
      const response = await fetch(`/api/evaluation/metrics/${encodeURIComponent(metricId)}/revise`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ record: buildMetricDefinition(form) }),
      });
      const data = await response.json() as { record_checksum?: string; error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? "The draft could not revise");
      toast({ type: "success", message: `Draft ${metricId} revised; checksum ${(data.record_checksum ?? "").slice(0, 12)}.` });
      setEditing(false);
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "The draft could not revise");
    } finally {
      setRevising(false);
    }
  };
  const steps = useMemo(() => lifecycleSteps(state), [state]);
  const transitions = useMemo(() => nextTransitions(state), [state]);
  const calibration = useMemo(() => stored ? calibrationSummary(stored.record) : null, [stored]);
  const advance = async (target: LifecycleState) => {
    setPending(target);
    setError(null);
    try {
      const response = await fetch(`/api/evaluation/metrics/${encodeURIComponent(metricId)}/advance`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(advanceRequest(target, { now: isoNow(), evidence, reason })),
      });
      const data = await response.json() as { lifecycle_state?: string; error?: string; detail?: string };
      if (!response.ok) throw new Error(data.error ?? data.detail ?? `The definition could not move to ${target}`);
      toast({ type: "success", message: `Metric ${metricId} is now ${lifecycleLabel(data.lifecycle_state ?? target).toLowerCase()}.` });
      await load();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : `The definition could not move to ${target}`);
    } finally {
      setPending(null);
    }
  };
  if (!stored && !error) return <div className="page-loading">Loading metric definition…</div>;
  if (!stored) return <ResourceState kind="unavailable" title="Metric definition unavailable" description={error ?? "The metric definition is unavailable."} onRetry={load} />;
  const record = stored.record;
  return (
    <div className="benchmarks-page">
      <BackLink href="/metrics" label="Metric definitions" />
      <header className="page-header">
        <div><p className="page-eyebrow">Metric definition</p><h2>{stored.metric_id}</h2><p>{record.measurement.numerator} over {record.measurement.denominator} ({record.measurement.unit}), {statusLabel(record.measurement.direction)}.</p></div>
        <div className="page-header__actions">
          <ActionButton variant="secondary" onClick={() => void load()}><RefreshCw size={15} /> Refresh</ActionButton>
          {state === "draft" ? <ActionButton variant={editing ? "secondary" : "primary"} onClick={() => (editing ? setEditing(false) : startEditing())}>{editing ? <X size={15} /> : <Pencil size={15} />} {editing ? "Close editor" : "Edit draft"}</ActionButton> : null}
        </div>
      </header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      {editing && form ? (
        <form className="benchmark-form metric-form" onSubmit={revise} aria-labelledby="metric-revise-title">
          <header><div><h3 id="metric-revise-title">Revise the draft</h3><p>A draft revises in place and keeps its id. Validation, publication, and retirement stay lifecycle transitions.</p></div></header>
          <MetricDefinitionFields form={form} update={(patch: Partial<MetricDefinitionForm>) => setForm((current) => (current ? { ...current, ...patch } : current))} scorers={scorers} lockIdentity />
          {formErrors.length ? <ul className="benchmark-report__warnings metric-form__errors" aria-label="Definition problems">{formErrors.map((entry) => <li key={entry}>{entry}</li>)}</ul> : null}
          <div className="benchmark-form__actions"><ActionButton type="submit" loading={revising} disabled={formErrors.length > 0}>Save draft revision</ActionButton></div>
        </form>
      ) : null}
      <section className="benchmark-catalog metric-lifecycle" aria-labelledby="metric-lifecycle-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="metric-lifecycle-title">Lifecycle</h3><span>Draft, validated, published, then deprecated or withdrawn. Each step is one immutable transition.</span></div></header>
        <ol className="metric-lifecycle__steps" aria-label="Lifecycle steps">
          {steps.map((step) => <li key={step.state} className={`metric-lifecycle__step metric-lifecycle__step--${step.status}`} aria-current={step.status === "current" || step.status === "terminal" ? "step" : undefined}><span className="metric-lifecycle__marker" aria-hidden="true" /><span>{step.label}</span></li>)}
        </ol>
        <div className="metric-lifecycle__actions">
          {transitions.includes("validated") ? (
            <div className="metric-lifecycle__evidence" aria-label="Validation evidence">
              {(["schema", "fixture", "evidence"] as const).map((name) => <label key={name}><input type="checkbox" checked={evidence[name]} onChange={(event) => setEvidence((current) => ({ ...current, [name]: event.target.checked }))} /> {name === "schema" ? "Schema check passed" : name === "fixture" ? "Fixture check passed" : "Evidence check passed"}</label>)}
            </div>
          ) : null}
          {transitions.includes("deprecated") ? <label className="metric-lifecycle__reason">Reason<input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Why the definition retires" /></label> : null}
          <div className="page-header__actions">
            {transitions.map((target) => <ActionButton key={target} variant={target === "published" || target === "validated" ? "primary" : target === "withdrawn" ? "danger" : "secondary"} loading={pending === target} disabled={(target === "deprecated" || target === "withdrawn") && !reason.trim()} onClick={() => void advance(target)}><ArrowRight size={15} /> {TRANSITION_LABELS[target]}</ActionButton>)}
            {transitions.length === 0 ? <span className="benchmark-status benchmark-status--cancelled">{lifecycleLabel(state)} is terminal</span> : null}
          </div>
        </div>
      </section>
      <section className="benchmark-catalog" aria-labelledby="metric-calibration-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="metric-calibration-title">Calibration</h3><span>Publication requires a complete calibration block in a current state.</span></div></header>
        {calibration ? (
          <dl className="benchmark-metadata metric-calibration">
            <div><dt>State</dt><dd><span className={`benchmark-status benchmark-status--${calibration.state === "current" ? "passed" : calibration.state === "failed" ? "failed" : "indeterminate"}`}>{statusLabel(calibration.state)}</span></dd></div>
            <div><dt>Method</dt><dd>{calibration.method} <small>v{calibration.version}</small></dd></div>
            <div><dt>Calibrated</dt><dd>{calibration.calibratedAt ?? "Unavailable"}</dd></div>
            <div><dt>Expires</dt><dd>{calibration.expiresAt ?? "Unavailable"}</dd></div>
            <div><dt>Contract</dt><dd>{calibration.complete ? "Complete" : `Missing ${calibration.missing.join(", ")}`}</dd></div>
          </dl>
        ) : null}
      </section>
      <section className="benchmark-catalog" aria-labelledby="metric-contract-title">
        <header className="dataset-catalog__toolbar"><div><h3 id="metric-contract-title">Contract</h3><span>Checksum <code>{stored.record_checksum.slice(0, 12)}</code></span></div></header>
        <dl className="benchmark-metadata metric-contract">
          <div><dt>Scorer</dt><dd>{record.scorer.scorer_id} <small>v{record.scorer.version}</small></dd></div>
          <div><dt>Population</dt><dd>{record.population.target}<small>{record.population.inclusion_rule}</small></dd></div>
          <div><dt>Labels</dt><dd>{record.labels.source}<small>{record.labels.evidence_contract.join(", ")}</small></dd></div>
          <div><dt>Missingness</dt><dd>{statusLabel(record.missingness)}</dd></div>
          <div><dt>Exclusions</dt><dd>{record.exclusions.length ? record.exclusions.join(", ") : "None"}</dd></div>
          <div><dt>Uncertainty</dt><dd>{statusLabel(record.uncertainty_method)}</dd></div>
          <div><dt>Aggregation</dt><dd>{statusLabel(record.measurement.aggregation)}</dd></div>
          <div><dt>Range</dt><dd>{record.measurement.range.minimum} to {record.measurement.range.maximum}</dd></div>
        </dl>
      </section>
    </div>
  );
}
