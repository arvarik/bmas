"use client";

import { Select } from "@/components/ui/Select";
import type { MetricDefinitionForm } from "@/lib/metric-lifecycle-presentation";

export interface ScorerOption {
  id: string;
  name?: string;
  version?: string;
}

interface Props {
  form: MetricDefinitionForm;
  update: (patch: Partial<MetricDefinitionForm>) => void;
  scorers: ScorerOption[];
  lockIdentity?: boolean;
}

/** The complete metric definition fields, shared by registration and draft revision. */
export function MetricDefinitionFields({ form, update, scorers, lockIdentity = false }: Props) {
  const scorerKnown = scorers.some((scorer) => scorer.id === form.scorer_id);
  return (
    <>
      <div className="benchmark-form__grid">
        <label>Metric id<input required value={form.metric_id} readOnly={lockIdentity} onChange={(event) => update({ metric_id: event.target.value })} placeholder="metric-task-success" /></label>
        <label>Scorer<Select required value={form.scorer_id} onChange={(event) => update({ scorer_id: event.target.value })}><option value="">Select a scorer</option>{!scorerKnown && form.scorer_id ? <option value={form.scorer_id}>{form.scorer_id}</option> : null}{scorers.map((scorer) => <option key={scorer.id} value={scorer.id}>{scorer.name ?? scorer.id}</option>)}</Select></label>
        <label>Scorer version<input required value={form.scorer_version} onChange={(event) => update({ scorer_version: event.target.value })} /></label>
        <label>Configuration digest<input required value={form.configuration_digest} onChange={(event) => update({ configuration_digest: event.target.value })} pattern="[a-f0-9]{64}" /></label>
      </div>
      <fieldset className="benchmark-form__section"><legend>Measurement</legend><div className="benchmark-form__grid">
        <label>Numerator<input required value={form.numerator} onChange={(event) => update({ numerator: event.target.value })} /></label>
        <label>Denominator<input required value={form.denominator} onChange={(event) => update({ denominator: event.target.value })} /></label>
        <label>Unit<input required value={form.unit} onChange={(event) => update({ unit: event.target.value })} /></label>
        <label>Aggregation<input required value={form.aggregation} onChange={(event) => update({ aggregation: event.target.value })} /></label>
        <label>Range minimum<input required type="number" step="any" value={form.range_minimum} onChange={(event) => update({ range_minimum: Number(event.target.value) })} /></label>
        <label>Range maximum<input required type="number" step="any" value={form.range_maximum} onChange={(event) => update({ range_maximum: Number(event.target.value) })} /></label>
        <label>Direction<Select value={form.direction} onChange={(event) => update({ direction: event.target.value as MetricDefinitionForm["direction"] })}><option value="higher_is_better">Higher is better</option><option value="lower_is_better">Lower is better</option></Select></label>
        <label>Uncertainty method<input required value={form.uncertainty_method} onChange={(event) => update({ uncertainty_method: event.target.value })} /></label>
      </div></fieldset>
      <fieldset className="benchmark-form__section"><legend>Population, labels, and missingness</legend><div className="benchmark-form__grid">
        <label>Population target<input required value={form.population_target} onChange={(event) => update({ population_target: event.target.value })} /></label>
        <label>Inclusion rule<input required value={form.inclusion_rule} onChange={(event) => update({ inclusion_rule: event.target.value })} /></label>
        <label>Label source<input required value={form.label_source} onChange={(event) => update({ label_source: event.target.value })} /></label>
        <label>Evidence contract fields (comma separated)<input required value={form.evidence_contract} onChange={(event) => update({ evidence_contract: event.target.value })} /></label>
        <label>Missingness policy<input required value={form.missingness} onChange={(event) => update({ missingness: event.target.value })} /></label>
        <label>Exclusions (comma separated)<input value={form.exclusions} onChange={(event) => update({ exclusions: event.target.value })} /></label>
      </div></fieldset>
      <fieldset className="benchmark-form__section"><legend>Calibration</legend><div className="benchmark-form__grid">
        <label>Method<input required value={form.calibration_method} onChange={(event) => update({ calibration_method: event.target.value })} /></label>
        <label>Version<input required value={form.calibration_version} onChange={(event) => update({ calibration_version: event.target.value })} /></label>
        <label>Dataset<input required value={form.calibration_dataset} onChange={(event) => update({ calibration_dataset: event.target.value })} /></label>
        <label>Drift policy<input required value={form.drift_policy} onChange={(event) => update({ drift_policy: event.target.value })} /></label>
        <label>Calibrated at<input required value={form.calibrated_at} onChange={(event) => update({ calibrated_at: event.target.value })} /></label>
        <label>Expires at<input required value={form.expires_at} onChange={(event) => update({ expires_at: event.target.value })} /></label>
      </div></fieldset>
    </>
  );
}
