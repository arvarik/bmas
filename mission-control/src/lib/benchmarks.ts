export type BenchmarkStatus =
  | "queued"
  | "running"
  | "paused"
  | "cancelling"
  | "cancelled"
  | "completed"
  | "failed"
  | "partial";

export interface BenchmarkScorer {
  id: string;
  name: string;
  version: string;
  description: string;
  kind: string;
}

export interface BenchmarkArm {
  id: string;
  name: string;
  slug: string;
  runtime_id: string;
  configuration: Record<string, unknown>;
  configuration_checksum: string;
}

export interface BenchmarkRevision {
  id: string;
  revision: number;
  dataset_version_id: string;
  dataset_name: string;
  dataset_version: number;
  item_count: number;
  configuration: Record<string, unknown>;
  configuration_checksum: string;
  published_at: string;
  arms: BenchmarkArm[];
  scorers: BenchmarkScorer[];
  runs: BenchmarkRun[];
}

export interface BenchmarkTest {
  id: string;
  name: string;
  description: string;
  latest_revision_id?: string;
  latest_revision?: number;
  dataset_name?: string;
  item_count?: number;
  arm_count?: number;
  run_count?: number;
  revisions?: BenchmarkRevision[];
}

export interface BenchmarkScore {
  id: string;
  attempt_id: string;
  scorer_id: string;
  scorer_name: string;
  scorer_version: string;
  status: string;
  score: number | null;
  passed: number | null;
  explanation: string | null;
  evidence: Record<string, unknown>;
}

export interface BenchmarkAttempt {
  id: string;
  trial_id: string;
  arm_name: string;
  item_key: string;
  repeat_index: number;
  retry_index: number;
  status: string;
  task_id: string | null;
  failure_category: string | null;
  error_message: string | null;
  total_cost_usd: number | null;
  total_tokens: number | null;
  duration_ms: number | null;
  result_summary: string | null;
  subject?: string | null;
  split?: string | null;
  tags?: string[];
}

export interface BenchmarkNamedMetric {
  scorer_id: string;
  scorer_name: string;
  mean: number | null;
  count: number;
}

export interface BenchmarkRunAggregates {
  total_cost_usd: number;
  failed_attempts: number;
  primary_metric: BenchmarkNamedMetric | null;
  secondary_metrics: BenchmarkNamedMetric[];
}

export interface BenchmarkRun {
  id: string;
  test_id: string;
  test_name: string;
  test_revision_id: string;
  revision: number;
  status: BenchmarkStatus;
  scoring_status?: "pending" | "running" | "completed" | "failed";
  analysis_status?: "pending" | "blocked" | "valid";
  total_trials: number;
  completed_trials: number;
  total_attempts: number;
  completed_attempts: number;
  total_cost_usd?: number;
  primary_scorer_id?: string | null;
  primary_scorer_name?: string | null;
  primary_metric_mean?: number | null;
  primary_metric_count?: number | null;
  failed_attempts?: number;
  aggregates?: BenchmarkRunAggregates;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  priority?: number;
  priority_band?: "expedited" | "standard" | "deferred";
  cost_status?: "provisional" | "settling" | "settled";
  settled_cost?: BenchmarkMoney | null;
  cost_bound?: BenchmarkMoney | null;
  dataset_name?: string;
  attempts?: BenchmarkAttempt[];
  scores?: BenchmarkScore[];
  human_reviews?: BenchmarkHumanReview[];
}

export interface BenchmarkHumanReview {
  id: string;
  attempt_id: string;
  reviewer_id: string;
  score: number;
  passed: number;
  note: string;
  created_at: string;
}

export interface BenchmarkMetricSummary {
  count: number;
  mean: number | null;
  total?: number | null;
  p50?: number | null;
  p95?: number | null;
  ci_low: number | null;
  ci_high: number | null;
  standard_error?: number | null;
  interval_status?: string;
}

export interface BenchmarkScorerMetric extends BenchmarkMetricSummary {
  scorer_id: string;
  scorer_name: string;
  scorer_version: string;
  passed: number;
  failed: number;
  excluded: number;
}

export interface BenchmarkArmReport {
  arm_id: string;
  arm_name: string;
  arm_slug: string;
  runtime_id: string;
  attempt_count: number;
  completed_count: number;
  failure_count: number;
  failure_rate: number | null;
  cost_usd: BenchmarkMetricSummary;
  duration_ms: BenchmarkMetricSummary;
  tokens: BenchmarkMetricSummary;
  scorers: BenchmarkScorerMetric[];
}

export interface BenchmarkComparisonMetric extends BenchmarkMetricSummary {
  scorer_id: string;
  wins: number;
  ties: number;
  losses: number;
  direction: "right_minus_left";
  probability_of_superiority: number | null;
  standardized_paired_effect: number | null;
  p_value_raw: number | null;
  p_value_adjusted: number | null;
  practical_difference: number;
  classification: string;
  sample_guidance: {
    method: string;
    practical_difference: number;
    recommended_pairs: number | null;
    reason: string | null;
  };
}

export interface BenchmarkComparison {
  left_arm_id: string;
  left_arm_name: string;
  right_arm_id: string;
  right_arm_name: string;
  left_arm_slug: string;
  right_arm_slug: string;
  matched_attempts: number;
  scorers: BenchmarkComparisonMetric[];
}

export interface BenchmarkRunReport {
  schema_version: string;
  interval_method: string;
  analysis: {
    version: string;
    confidence_level: number;
    interval_method: string;
    bootstrap_resamples: number;
    paired_test: string;
    multiple_comparison_method: string;
    family_alpha: number;
    practical_difference: number;
  };
  run: {
    id: string;
    status: BenchmarkStatus;
    test_id: string;
    test_revision_id: string;
    test_configuration_checksum: string;
    dataset_id: string;
    dataset_checksum: string;
    execution_plan_checksum: string;
  };
  filters: Record<string, string>;
  latest_attempt_count: number;
  prior_attempt_count: number;
  arms: BenchmarkArmReport[];
  comparisons: BenchmarkComparison[];
  diagnostics: {
    error_categories: Array<{
      arm_id: string;
      arm_name: string;
      category: string;
      count: number;
      rate: number;
    }>;
    slices: Array<{
      dimension: string;
      value: string;
      attempt_count: number;
      arms: Array<{
        arm_id: string;
        arm_name: string;
        attempt_count: number;
        failure_rate: number;
        scorers: Array<BenchmarkMetricSummary & { scorer_id: string }>;
      }>;
    }>;
    item_differences: Array<{
      left_arm_id: string;
      right_arm_id: string;
      scorer_id: string;
      dataset_item_id: string;
      item_key: string;
      repeat_index: number;
      delta: number;
    }>;
    item_difference_count: number;
    item_differences_truncated: boolean;
    human_review: {
      available: boolean;
      reviewed_attempt_count: number;
      review_count: number;
      reason: string | null;
    };
    human_calibration: Array<{
      scorer_id: string;
      count: number;
      agreement_rate: number;
      cohen_kappa: number | null;
      mean_absolute_error: number;
      brier_score: number;
    }>;
    scorer_agreement: Array<{
      left_scorer_id: string;
      right_scorer_id: string;
      count: number;
      agreement_rate: number;
      cohen_kappa: number | null;
    }>;
  };
  warnings: string[];
  complete: boolean;
  report_checksum: string;
  /** The legacy engine names itself once a frozen engine exists. */
  engine?: "legacy-report";
  frozen_snapshot?: null;
  statement?: string;
}

/** One frozen comparison: the predeclared estimate, interval, test, and gate. */
export interface FrozenComparison {
  comparison_id: string;
  metric: string;
  baseline_arm: string;
  candidate_arm: string;
  direction: "higher_is_better" | "lower_is_better";
  hypothesis: "non_inferiority" | "superiority";
  non_inferiority_margin: number | null;
  minimum_usable_cases: number;
  estimate: number | null;
  family_aggregates?: Record<string, number>;
  interval: FrozenInterval;
  test: FrozenTest;
  p_value_adjusted: number | null;
  multiplicity_family?: string;
  counts: { paired_cases: number; missing_cases: number; removed_slots: number };
  total_missing_weight?: number;
  limit_failures: string[];
  primary_valid: boolean;
  small_families: string[];
  comparative_claim: boolean;
  statistical_unit: string;
  gate: FrozenGateDecision;
}

export interface FrozenArmSummary {
  counts: Record<string, number>;
  unconditional_denominator: number;
  unconditional_successes: number;
  unconditional_success_rate: number | null;
  denominator_statement: string;
  latency_ms?: { count: number; median_ms: number | null; p95_ms: number | null; estimator?: string };
}

export interface ResolvedMetricDefinition {
  metric_id: string;
  calibration_version: string;
  lifecycle_state: string;
  scorer: { scorer_id: string; version: string; configuration_digest: string };
  measurement: {
    numerator: string;
    denominator: string;
    unit: string;
    range: { minimum: number; maximum: number };
    direction: string;
    aggregation: string;
  };
}

export interface FrozenResourceAnalytics {
  available: boolean;
  statement?: string;
  currency?: string;
  actual_total?: BenchmarkMoney;
  estimate_total?: BenchmarkMoney;
  unknown_entry_ids?: string[];
  cost_per_success?: BenchmarkMoney | null;
  unconditional_successes?: number;
}

/** The frozen snapshot the report route serves once one exists. */
export interface FrozenRunReport {
  engine: "bmas-frozen-analysis";
  engine_version: string;
  snapshot_id: string;
  replay_verified: boolean;
  results_digest: string;
  stored_results_digest: string;
  metrics: ResolvedMetricDefinition[];
  unresolved_metrics: Array<{ metric_id: string; reason: string }>;
  analysis: {
    estimand: string;
    statistical_unit: string;
    specification_digest: string;
    replay_claim: string;
  };
  denominators: { planned: number; statement: string };
  comparisons: FrozenComparison[];
  arms: Record<string, FrozenArmSummary>;
  resources: FrozenResourceAnalytics;
  warnings: string[];
  report: { metric_ids: string[]; results_digest: string; input_digest: string; [key: string]: unknown };
}

export type RunReportResponse = BenchmarkRunReport | FrozenRunReport;

export function isFrozenReport(report: RunReportResponse | null | undefined): report is FrozenRunReport {
  return Boolean(report && (report as FrozenRunReport).engine === "bmas-frozen-analysis");
}

export type RegressionOperator = "gte" | "lte" | "max_drop" | "max_increase_ratio";
export type RegressionAnalysisMethod =
  | "point_estimate"
  | "lower_confidence_bound"
  | "upper_confidence_bound"
  | "holm_sign_test"
  | "frozen_non_inferiority"
  | "frozen_superiority";
export type RegressionDirection = "improvement" | "reduction";

/** The frozen methods decide through the frozen analysis engine. */
export const FROZEN_ANALYSIS_METHODS: readonly RegressionAnalysisMethod[] = [
  "frozen_non_inferiority",
  "frozen_superiority",
];
export const FROZEN_METRIC_PREFIX = "frozen.";

export interface RegressionRule {
  id: string;
  label: string;
  metric: string;
  operator: RegressionOperator;
  value: number;
  analysis_method?: RegressionAnalysisMethod;
  direction?: RegressionDirection | null;
  practical_size?: number | null;
  minimum_usable_cases?: number | null;
  resample_count?: number | null;
}

/** The frozen interval, test, and decision behind one frozen comparison. */
export interface FrozenInterval {
  status: string;
  low: number | null;
  high: number | null;
  method?: string;
  unit?: string;
  replicate_count?: number;
  reason?: string;
}

export interface FrozenTest {
  method: string;
  mode: string | null;
  p_value: number | null;
  resamples: number;
}

export interface FrozenGateDecision {
  status: "passed" | "failed" | "indeterminate";
  reasons: string[];
  bound?: number | null;
  margin?: number | null;
  rule?: string;
  p_value_adjusted?: number | null;
}

export interface FrozenRuleBlock {
  engine: string;
  engine_version?: string;
  reason?: string;
  specification_digest?: string;
  input_digest?: string;
  results_digest?: string;
  baseline_arm?: string;
  candidate_arm?: string;
  estimate?: number | null;
  interval?: FrozenInterval;
  test?: FrozenTest;
  p_value_adjusted?: number | null;
  gate?: FrozenGateDecision;
  counts?: { paired_cases: number; missing_cases: number; removed_slots: number };
  statistical_unit?: string;
}

export interface BenchmarkGateRuleResult extends RegressionRule {
  threshold: number;
  baseline_value: number | null;
  candidate_value: number | null;
  boundary: number | null;
  status: "passed" | "failed" | "indeterminate" | "waived_display";
  frozen?: FrozenRuleBlock;
}

export interface BenchmarkGateReport {
  status: "passed" | "failed" | "indeterminate";
  reason: string;
  mode?: "preview" | "final";
  baseline_run_id: string;
  candidate_run_id: string;
  rules: BenchmarkGateRuleResult[];
  engines?: string[];
  report_checksum: string;
}

export interface BenchmarkGatePreview {
  report: BenchmarkGateReport;
  saved: false;
}

export interface BenchmarkGateEvaluation {
  id: string;
  baseline_id: string;
  candidate_run_id: string;
  status: "passed" | "failed" | "indeterminate";
  report: BenchmarkGateReport;
  report_checksum: string;
  created_at?: string;
}

export interface BenchmarkBaseline {
  id: string;
  test_id: string;
  test_name: string;
  run_id: string;
  run_status: BenchmarkStatus;
  name: string;
  description: string;
  rules: RegressionRule[];
  rules_checksum: string;
  created_by: string;
  created_at: string;
  latest_gate_status?: string | null;
  evaluation_count?: number;
  evaluations?: BenchmarkGateEvaluation[];
}

export interface RuntimeBenchmarkContract {
  supported: boolean;
  configuration_schema: Record<string, unknown>;
  seed_strategy: string;
  supports_repetitions: boolean;
  required_snapshot_fields: string[];
}

export interface BenchmarkRuntime {
  id: string;
  label: string;
  available: boolean;
  contract_version: string;
  configuration_schema_version: string;
  supports_recovery: boolean;
  benchmark: RuntimeBenchmarkContract;
}

export interface RuntimeQualificationCheck {
  name: string;
  status: "passed" | "failed";
  detail: string;
}

export interface RuntimeQualificationReport {
  runtime_id: string;
  runtime_label: string;
  contract_version: string;
  status: "provisional" | "passed" | "failed";
  evidence_status: "not_run" | "passed" | "failed";
  run_id: string | null;
  checks: RuntimeQualificationCheck[];
  report_checksum: string;
}

export interface RuntimeQualification {
  id: string;
  runtime_id: string;
  contract_version: string;
  run_id: string | null;
  status: "provisional" | "passed" | "failed";
  report: RuntimeQualificationReport;
  report_checksum: string;
  created_at?: string;
}

export interface BenchmarkRuntimeCatalog {
  api_version: string;
  variants: BenchmarkRuntime[];
  qualifications: RuntimeQualification[];
  planned_runtime_ids: string[];
}

export interface BenchmarkCapacityResource {
  key: string;
  active: number;
  limit: number;
  available: number;
}

export interface BenchmarkSchedulerWorker {
  worker_id: string;
  hostname: string;
  process_id: number;
  status: "active" | "stopped";
  last_seen_at: string;
  stale: number;
  owned_attempts: number;
}

export interface BenchmarkCapacity {
  schema_version: string;
  global: { active: number; limit: number; available: number };
  resources: BenchmarkCapacityResource[];
  unlimited_active_resources: Array<{ key: string; active: number }>;
  queue: {
    total: number;
    by_priority: Array<{ priority: number; count: number }>;
  };
  workers: BenchmarkSchedulerWorker[];
}

export function runProgress(run: Pick<BenchmarkRun, "completed_attempts" | "total_attempts">) {
  if (run.total_attempts <= 0) return 0;
  return Math.min(100, Math.round((run.completed_attempts / run.total_attempts) * 100));
}

export function statusLabel(status: string) {
  return status.replaceAll("_", " ").replace(/^./, (value) => value.toUpperCase());
}

export function formatMetric(value: number | null | undefined, unit: "percent" | "cost" | "duration" | "tokens") {
  if (value === null || value === undefined) return "Unavailable";
  if (unit === "percent") return `${(value * 100).toFixed(1)}%`;
  if (unit === "cost") return `$${value.toFixed(4)}`;
  if (unit === "duration") return `${(value / 1000).toFixed(2)}s`;
  return Math.round(value).toLocaleString();
}

/**
 * The frozen metric paths a baseline can gate: one per scorer across the
 * compared arm, and one per scorer and arm slug.
 */
export function frozenMetricOptions(report: RunReportResponse): Array<{ value: string; label: string }> {
  const scorers = new Map<string, string>();
  const arms: Array<{ slug: string; name: string }> = [];
  if (isFrozenReport(report)) {
    for (const comparison of report.comparisons) scorers.set(comparison.metric, comparison.metric);
    for (const slug of Object.keys(report.arms)) arms.push({ slug, name: slug });
  } else {
    for (const arm of report.arms) {
      arms.push({ slug: arm.arm_slug, name: arm.arm_name });
      for (const scorer of arm.scorers) scorers.set(scorer.scorer_id, scorer.scorer_name);
    }
  }
  return [...scorers.entries()].flatMap(([scorerId, scorerName]) => [
    { value: `${FROZEN_METRIC_PREFIX}${scorerId}`, label: `Frozen ${scorerName} across runs (first arm)` },
    ...arms.map((arm) => ({
      value: `${FROZEN_METRIC_PREFIX}${scorerId}.${arm.slug}`,
      label: `Frozen ${scorerName} across runs (${arm.name})`,
    })),
  ]);
}

export function reportMetricOptions(report: RunReportResponse): Array<{ value: string; label: string }> {
  if (isFrozenReport(report)) return frozenMetricOptions(report);
  const armOptions = report.arms.flatMap((arm) => [
    { value: `arm.${arm.arm_slug}.failure_rate`, label: `${arm.arm_name} failure rate` },
    { value: `arm.${arm.arm_slug}.cost_usd.mean`, label: `${arm.arm_name} mean cost` },
    { value: `arm.${arm.arm_slug}.duration_ms.p95`, label: `${arm.arm_name} p95 duration` },
    ...arm.scorers.map((scorer) => ({
      value: `arm.${arm.arm_slug}.score.${scorer.scorer_id}`,
      label: `${arm.arm_name} ${scorer.scorer_name} score`,
    })),
  ]);
  const comparisonOptions = (report.comparisons ?? []).flatMap((comparison) =>
    comparison.scorers.map((scorer) => ({
      value: `comparison.${comparison.left_arm_slug}.${comparison.right_arm_slug}.score.${scorer.scorer_id}`,
      label: `${comparison.left_arm_name} to ${comparison.right_arm_name} ${scorer.scorer_id} paired difference`,
    })),
  );
  return [...armOptions, ...comparisonOptions, ...frozenMetricOptions(report)];
}

export function isFrozenMetric(metric: string): boolean {
  return metric.startsWith(FROZEN_METRIC_PREFIX);
}

export function supportedAnalysisMethods(metric: string): RegressionAnalysisMethod[] {
  if (isFrozenMetric(metric)) return [...FROZEN_ANALYSIS_METHODS];
  const methods: RegressionAnalysisMethod[] = ["point_estimate"];
  if (metric.includes(".score.") || metric.endsWith(".mean")) {
    methods.push("lower_confidence_bound", "upper_confidence_bound");
  }
  if (metric.startsWith("comparison.")) methods.push("holm_sign_test");
  return methods;
}

/** Read the server's named primary metric. The browser never averages. */
export function primaryMetric(run: BenchmarkRun): BenchmarkNamedMetric | null {
  if (run.aggregates?.primary_metric) return run.aggregates.primary_metric;
  if (run.primary_scorer_id && run.primary_metric_mean !== null && run.primary_metric_mean !== undefined) {
    return {
      scorer_id: run.primary_scorer_id,
      scorer_name: run.primary_scorer_name ?? run.primary_scorer_id,
      mean: run.primary_metric_mean,
      count: run.primary_metric_count ?? 0,
    };
  }
  return null;
}

/** One exact monetary amount: an ISO currency code and integer nanos. */
export interface BenchmarkMoney {
  currency: string;
  amount_nanos: number;
}

/** Render one exact amount without floating-point arithmetic. */
export function formatMoney(money: BenchmarkMoney): string {
  const negative = money.amount_nanos < 0;
  const magnitude = negative ? -money.amount_nanos : money.amount_nanos;
  const units = Math.trunc(magnitude / 1e9);
  const nanos = magnitude % 1e9;
  const fraction = String(nanos).padStart(9, "0").replace(/0+$/, "");
  const text = fraction ? `${units}.${fraction}` : String(units);
  return `${negative ? "-" : ""}${text} ${money.currency}`;
}

/** Label a scoring failure so failed work never looks complete. */
export function scoringBadge(run: BenchmarkRun): string | null {
  if (run.scoring_status === "failed") return "Scoring failed";
  if (run.analysis_status === "blocked") return "Analysis blocked";
  return null;
}

/** Describe the run cost settlement state for operators. */
export function costBadge(run: BenchmarkRun): string | null {
  if (run.cost_status === "settled" && run.settled_cost) {
    return `Cost settled: ${formatMoney(run.settled_cost)}`;
  }
  if (run.cost_status === "settling") return "Cost settling";
  return null;
}
