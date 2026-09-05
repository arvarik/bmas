"use client";

import { useEffect, useMemo, useState } from "react";
import { Snowflake } from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { Select } from "@/components/ui/Select";
import { useToast } from "@/hooks/useToast";
import type { BenchmarkRun } from "@/lib/benchmarks";
import { errorText } from "@/lib/evaluation-operations";
import { armOptions, buildFreezeRequest, defaultFreezeForm, familiesFromAttempts, freezeFormErrors, type FreezeForm } from "@/lib/freeze-presentation";
import type { StoredMetricDefinition } from "@/lib/metric-lifecycle-presentation";

interface ScorerOption {
  id: string;
  name?: string;
}

/**
 * Freeze one analysis snapshot from the browser.
 *
 * The families come from the run's attempts, the arms become the
 * comparison ends, and the published metric definitions resolve the
 * displayed metrics. The daemon stores the snapshot with its replay
 * claim, and the analysis history reloads.
 */
export function FreezeAnalysisForm({ run, onFrozen }: { run: BenchmarkRun; onFrozen: () => void }) {
  const { toast } = useToast();
  const attempts = useMemo(() => run.attempts ?? [], [run.attempts]);
  const [scorers, setScorers] = useState<ScorerOption[]>([]);
  const [metrics, setMetrics] = useState<StoredMetricDefinition[]>([]);
  const [form, setForm] = useState<FreezeForm>(() => defaultFreezeForm(attempts, run.primary_scorer_id ?? ""));
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let active = true;
    void (async () => {
      const [scorerResponse, metricResponse] = await Promise.all([
        fetch("/api/benchmarks/scorers", { cache: "no-store" }),
        fetch("/api/evaluation/metrics?lifecycle_state=published", { cache: "no-store" }),
      ]);
      if (!active) return;
      if (scorerResponse.ok) {
        const data = await scorerResponse.json() as { scorers?: ScorerOption[] };
        setScorers(data.scorers ?? []);
      }
      if (metricResponse.ok) {
        const data = await metricResponse.json() as { metrics?: StoredMetricDefinition[] };
        setMetrics(data.metrics ?? []);
      }
    })();
    return () => { active = false; };
  }, []);
  const arms = useMemo(() => armOptions(attempts), [attempts]);
  const families = useMemo(() => familiesFromAttempts(attempts), [attempts]);
  const errors = useMemo(() => freezeFormErrors(form, attempts), [attempts, form]);
  const update = (patch: Partial<FreezeForm>) => setForm((current) => ({ ...current, ...patch }));
  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (errors.length) return;
    setPending(true);
    setError(null);
    try {
      const response = await fetch(`/api/evaluation/runs/${encodeURIComponent(run.id)}/analyses/freeze`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(buildFreezeRequest(form, attempts)),
      });
      const data = await response.json() as { snapshot_id?: string; replay?: { claim?: string }; error?: string; detail?: string };
      if (!response.ok) throw new Error(errorText(data, "The analysis could not freeze"));
      toast({ type: "success", message: `Snapshot ${data.snapshot_id ?? "stored"} frozen${data.replay?.claim ? ` with claim ${data.replay.claim.replaceAll("_", " ")}` : ""}.` });
      onFrozen();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "The analysis could not freeze");
    } finally {
      setPending(false);
    }
  };
  return (
    <form className="benchmark-form freeze-form" onSubmit={submit} aria-labelledby="freeze-form-title">
      <header><div><h4 id="freeze-form-title"><Snowflake size={15} /> Freeze an analysis snapshot</h4><p>The snapshot fixes the families, the seed, the resampling plan, the metric definitions, and one predeclared comparison. {Object.keys(families).length} famil{Object.keys(families).length === 1 ? "y" : "ies"} from {attempts.length} attempts.</p></div></header>
      {error ? <p className="benchmark-message benchmark-message--error" role="alert">{error}</p> : null}
      <div className="benchmark-form__grid">
        <label>Scorer<Select value={form.scorer_id} onChange={(event) => update({ scorer_id: event.target.value })}><option value="">Select a scorer</option>{scorers.map((scorer) => <option key={scorer.id} value={scorer.id}>{scorer.name ?? scorer.id}</option>)}</Select></label>
        <label>Master seed<input type="number" min={0} value={form.master_seed} onChange={(event) => update({ master_seed: Number(event.target.value) })} /></label>
        <label>Planned repetitions<input type="number" min={1} max={1000} value={form.planned_repetitions} onChange={(event) => update({ planned_repetitions: Number(event.target.value) })} /></label>
        <label>Resamples<input type="number" min={1} max={100000} value={form.resample_count} onChange={(event) => update({ resample_count: Number(event.target.value) })} /></label>
        <label>Confidence level<input type="number" min={0.5} max={0.999} step={0.005} value={form.confidence_level} onChange={(event) => update({ confidence_level: Number(event.target.value) })} /></label>
        <label>Binary reduction<Select value={form.binary_reduction} onChange={(event) => update({ binary_reduction: event.target.value })}><option value="strict_majority">Strict majority</option><option value="all">All repetitions pass</option><option value="any">Any repetition passes</option></Select></label>
      </div>
      <fieldset className="benchmark-form__section"><legend>Predeclared comparison</legend><div className="benchmark-form__grid">
        <label>Family id<input value={form.family_id} onChange={(event) => update({ family_id: event.target.value })} /></label>
        <label>Comparison id<input value={form.comparison_id} onChange={(event) => update({ comparison_id: event.target.value })} /></label>
        <label>Baseline arm<Select value={form.baseline_arm} onChange={(event) => update({ baseline_arm: event.target.value })}>{arms.map((arm) => <option key={arm.arm_id} value={arm.arm_id}>{arm.arm_name}</option>)}</Select></label>
        <label>Candidate arm<Select value={form.candidate_arm} onChange={(event) => update({ candidate_arm: event.target.value })}>{arms.map((arm) => <option key={arm.arm_id} value={arm.arm_id}>{arm.arm_name}</option>)}</Select></label>
        <label>Non-inferiority margin<input type="number" min={0} max={1} step={0.01} value={form.non_inferiority_margin} onChange={(event) => update({ non_inferiority_margin: Number(event.target.value) })} /></label>
      </div></fieldset>
      <fieldset className="benchmark-form__section freeze-form__metrics"><legend>Published metric definitions</legend>
        {metrics.length === 0 ? <p className="attempt-evidence__note">No published definition. The snapshot freezes without metric ids, and the frozen report serves only with unresolved metrics allowed.</p> : null}
        <div className="metric-lifecycle__evidence">{metrics.map((metric) => <label key={metric.metric_id}><input type="checkbox" checked={form.metric_ids.includes(metric.metric_id)} onChange={(event) => update({ metric_ids: event.target.checked ? [...form.metric_ids, metric.metric_id] : form.metric_ids.filter((id) => id !== metric.metric_id) })} /> {metric.metric_id}</label>)}</div>
      </fieldset>
      {errors.length ? <ul className="benchmark-report__warnings metric-form__errors" aria-label="Freeze problems">{errors.map((entry) => <li key={entry}>{entry}</li>)}</ul> : null}
      <div className="benchmark-form__actions"><ActionButton type="submit" loading={pending} disabled={errors.length > 0}>Freeze snapshot</ActionButton></div>
    </form>
  );
}
