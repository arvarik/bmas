/**
 * Presentation helpers for the metric definition lifecycle.
 *
 * A definition moves draft → validated → published, then to
 * deprecated or withdrawn. Publication needs one complete contract and
 * a current calibration, so the form collects every required field and
 * the stepper shows where a definition stands and which transitions
 * the daemon accepts next.
 */

export type LifecycleState = "draft" | "validated" | "published" | "deprecated" | "withdrawn";

export const LIFECYCLE_TRANSITIONS: Record<LifecycleState, LifecycleState[]> = {
  draft: ["validated"],
  validated: ["published", "draft"],
  published: ["deprecated", "withdrawn"],
  deprecated: [],
  withdrawn: [],
};

export const LIFECYCLE_ORDER: LifecycleState[] = ["draft", "validated", "published"];

export interface LifecycleStep {
  state: LifecycleState;
  label: string;
  status: "done" | "current" | "upcoming" | "terminal";
}

export function lifecycleLabel(state: string): string {
  return state.replace(/^./, (value) => value.toUpperCase());
}

/** The stepper: the three forward states plus one terminal state when reached. */
export function lifecycleSteps(state: LifecycleState): LifecycleStep[] {
  const terminal = state === "deprecated" || state === "withdrawn";
  const index = terminal ? LIFECYCLE_ORDER.length : LIFECYCLE_ORDER.indexOf(state);
  const steps: LifecycleStep[] = LIFECYCLE_ORDER.map((entry, position) => ({
    state: entry,
    label: lifecycleLabel(entry),
    status: position < index ? "done" : position === index ? "current" : "upcoming",
  }));
  if (terminal) steps.push({ state, label: lifecycleLabel(state), status: "terminal" });
  return steps;
}

export function nextTransitions(state: LifecycleState): LifecycleState[] {
  return LIFECYCLE_TRANSITIONS[state] ?? [];
}

export interface MetricDefinitionForm {
  metric_id: string;
  scorer_id: string;
  scorer_version: string;
  configuration_digest: string;
  numerator: string;
  denominator: string;
  unit: string;
  range_minimum: number;
  range_maximum: number;
  direction: "higher_is_better" | "lower_is_better";
  aggregation: string;
  population_target: string;
  inclusion_rule: string;
  label_source: string;
  evidence_contract: string;
  missingness: string;
  exclusions: string;
  uncertainty_method: string;
  calibration_method: string;
  calibration_version: string;
  calibration_dataset: string;
  calibrated_at: string;
  expires_at: string;
  drift_policy: string;
}

const DIGEST_PATTERN = /^[a-f0-9]{64}$/;
const IDENTIFIER_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,199}$/;

function isoDate(date: Date): string {
  return date.toISOString().replace(/\.\d{3}Z$/, "Z");
}

/** Sensible defaults: a deterministic calibration current for one year. */
export function defaultMetricForm(now: Date = new Date()): MetricDefinitionForm {
  const expires = new Date(now.getTime());
  expires.setUTCFullYear(expires.getUTCFullYear() + 1);
  return {
    metric_id: "",
    scorer_id: "",
    scorer_version: "1",
    configuration_digest: "0".repeat(64),
    numerator: "Cases with a passing binary reduction.",
    denominator: "Unconditional planned cases.",
    unit: "proportion",
    range_minimum: 0,
    range_maximum: 1,
    direction: "higher_is_better",
    aggregation: "family_stratified_weighted_mean",
    population_target: "declared dataset cases",
    inclusion_rule: "Every planned non-excluded slot counts.",
    label_source: "scorer",
    evidence_contract: "final_output",
    missingness: "predeclared_infrastructure_exclusions",
    exclusions: "",
    uncertainty_method: "family_stratified_weighted_case_bootstrap",
    calibration_method: "deterministic",
    calibration_version: "1",
    calibration_dataset: "calibration-fixtures",
    calibrated_at: isoDate(now),
    expires_at: isoDate(expires),
    drift_policy: "recalibrate-on-implementation-change",
  };
}

/** Every reason the form cannot become a valid definition yet. */
export function metricFormErrors(form: MetricDefinitionForm): string[] {
  const errors: string[] = [];
  if (!IDENTIFIER_PATTERN.test(form.metric_id)) errors.push("The metric id uses letters, digits, and - _ . : @ / only.");
  if (!IDENTIFIER_PATTERN.test(form.scorer_id)) errors.push("Select the scorer the metric reads.");
  if (!form.scorer_version.trim()) errors.push("The scorer version is required.");
  if (!DIGEST_PATTERN.test(form.configuration_digest)) errors.push("The configuration digest is 64 hex characters.");
  for (const [name, value] of [
    ["numerator", form.numerator], ["denominator", form.denominator], ["unit", form.unit],
    ["aggregation", form.aggregation], ["population target", form.population_target],
    ["inclusion rule", form.inclusion_rule], ["label source", form.label_source],
    ["missingness", form.missingness], ["uncertainty method", form.uncertainty_method],
    ["calibration method", form.calibration_method], ["calibration version", form.calibration_version],
    ["calibration dataset", form.calibration_dataset], ["drift policy", form.drift_policy],
  ] as const) {
    if (!value.trim()) errors.push(`The ${name} is required.`);
  }
  if (!(form.range_minimum < form.range_maximum)) errors.push("The range minimum stays below the maximum.");
  if (!form.evidence_contract.split(",").some((entry) => entry.trim())) errors.push("Name at least one evidence contract field.");
  if (Number.isNaN(Date.parse(form.calibrated_at)) || Number.isNaN(Date.parse(form.expires_at))) {
    errors.push("The calibration dates are ISO 8601 timestamps.");
  }
  return errors;
}

/** Build the metric-definition record the daemon contract validates. */
export function buildMetricDefinition(form: MetricDefinitionForm) {
  return {
    schema_id: "metric-definition",
    schema_version: 2,
    metric_id: form.metric_id.trim(),
    lifecycle_state: "draft",
    calibration: {
      state: "current",
      method: form.calibration_method.trim(),
      version: form.calibration_version.trim(),
      dataset: form.calibration_dataset.trim(),
      result: { limits_failed: false, pinned_digests: {} },
      calibrated_at: form.calibrated_at.trim(),
      expires_at: form.expires_at.trim(),
      drift_policy: form.drift_policy.trim(),
    },
    population: { target: form.population_target.trim(), inclusion_rule: form.inclusion_rule.trim() },
    measurement: {
      numerator: form.numerator.trim(),
      denominator: form.denominator.trim(),
      unit: form.unit.trim(),
      range: { minimum: form.range_minimum, maximum: form.range_maximum },
      direction: form.direction,
      aggregation: form.aggregation.trim(),
    },
    labels: {
      source: form.label_source.trim(),
      evidence_contract: form.evidence_contract.split(",").map((entry) => entry.trim()).filter(Boolean),
    },
    scorer: {
      scorer_id: form.scorer_id.trim(),
      version: form.scorer_version.trim(),
      configuration_digest: form.configuration_digest.trim(),
    },
    missingness: form.missingness.trim(),
    exclusions: form.exclusions.split(",").map((entry) => entry.trim()).filter(Boolean),
    uncertainty_method: form.uncertainty_method.trim(),
  };
}

export interface StoredMetricDefinition {
  metric_id: string;
  lifecycle_state: LifecycleState;
  calibration_state: string;
  record_checksum: string;
  created_at: string;
  record: ReturnType<typeof buildMetricDefinition> & { lifecycle_state: LifecycleState };
}

export interface CalibrationSummary {
  method: string;
  version: string;
  state: string;
  calibratedAt: string | null;
  expiresAt: string | null;
  complete: boolean;
  missing: string[];
}

/** What the calibration block holds and what publication still needs. */
export function calibrationSummary(record: { calibration?: Record<string, unknown> }): CalibrationSummary {
  const calibration = record.calibration ?? {};
  const required = ["dataset", "method", "result", "version", "calibrated_at", "expires_at", "drift_policy"];
  const missing = required.filter((field) => {
    const value = calibration[field];
    return value === undefined || value === null || value === "" || (typeof value === "object" && Object.keys(value as object).length === 0);
  });
  return {
    method: String(calibration.method ?? "unknown"),
    version: String(calibration.version ?? "?"),
    state: String(calibration.state ?? "unknown"),
    calibratedAt: typeof calibration.calibrated_at === "string" ? calibration.calibrated_at : null,
    expiresAt: typeof calibration.expires_at === "string" ? calibration.expires_at : null,
    complete: missing.length === 0,
    missing,
  };
}

/** The advance request body for one transition. */
export function advanceRequest(
  target: LifecycleState,
  options: { now: string; evidence?: { schema: boolean; fixture: boolean; evidence: boolean }; reason?: string },
) {
  const body: { target: LifecycleState; now: string; validation_evidence?: Record<string, boolean>; reason?: string } = {
    target,
    now: options.now,
  };
  if (target === "validated") body.validation_evidence = { ...(options.evidence ?? { schema: false, fixture: false, evidence: false }) };
  if (target === "deprecated" || target === "withdrawn") body.reason = options.reason ?? "";
  return body;
}

/** The form that reproduces one stored definition, for a draft revision. */
export function formFromRecord(record: StoredMetricDefinition["record"]): MetricDefinitionForm {
  const calibration = (record.calibration ?? {}) as Record<string, unknown>;
  const text = (value: unknown, fallback = ""): string => (typeof value === "string" ? value : fallback);
  return {
    metric_id: record.metric_id,
    scorer_id: record.scorer.scorer_id,
    scorer_version: record.scorer.version,
    configuration_digest: record.scorer.configuration_digest,
    numerator: record.measurement.numerator,
    denominator: record.measurement.denominator,
    unit: record.measurement.unit,
    range_minimum: record.measurement.range.minimum,
    range_maximum: record.measurement.range.maximum,
    direction: record.measurement.direction,
    aggregation: record.measurement.aggregation,
    population_target: record.population.target,
    inclusion_rule: record.population.inclusion_rule,
    label_source: record.labels.source,
    evidence_contract: record.labels.evidence_contract.join(", "),
    missingness: record.missingness,
    exclusions: record.exclusions.join(", "),
    uncertainty_method: record.uncertainty_method,
    calibration_method: text(calibration.method, "deterministic"),
    calibration_version: text(calibration.version, "1"),
    calibration_dataset: text(calibration.dataset, "calibration-fixtures"),
    calibrated_at: text(calibration.calibrated_at),
    expires_at: text(calibration.expires_at),
    drift_policy: text(calibration.drift_policy, "recalibrate-on-implementation-change"),
  };
}
