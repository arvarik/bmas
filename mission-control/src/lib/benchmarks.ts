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

export interface BenchmarkRun {
  id: string;
  test_id: string;
  test_name: string;
  test_revision_id: string;
  revision: number;
  status: BenchmarkStatus;
  total_trials: number;
  completed_trials: number;
  total_attempts: number;
  completed_attempts: number;
  total_cost_usd?: number;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  dataset_name?: string;
  attempts?: BenchmarkAttempt[];
  scores?: BenchmarkScore[];
}

export interface BenchmarkMetricSummary {
  count: number;
  mean: number | null;
  total?: number | null;
  p50?: number | null;
  p95?: number | null;
  ci_low: number | null;
  ci_high: number | null;
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
}

export interface BenchmarkComparison {
  left_arm_id: string;
  left_arm_name: string;
  right_arm_id: string;
  right_arm_name: string;
  matched_attempts: number;
  scorers: BenchmarkComparisonMetric[];
}

export interface BenchmarkRunReport {
  schema_version: string;
  interval_method: "normal_95";
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
  complete: boolean;
  report_checksum: string;
}

export type RegressionOperator = "gte" | "lte" | "max_drop" | "max_increase_ratio";

export interface RegressionRule {
  id: string;
  label: string;
  metric: string;
  operator: RegressionOperator;
  value: number;
}

export interface BenchmarkGateRuleResult extends RegressionRule {
  threshold: number;
  baseline_value: number | null;
  candidate_value: number | null;
  boundary: number | null;
  status: "passed" | "failed" | "indeterminate";
}

export interface BenchmarkGateReport {
  status: "passed" | "failed" | "indeterminate";
  reason: string;
  baseline_run_id: string;
  candidate_run_id: string;
  rules: BenchmarkGateRuleResult[];
  report_checksum: string;
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

export function reportMetricOptions(report: BenchmarkRunReport): Array<{ value: string; label: string }> {
  return report.arms.flatMap((arm) => [
    { value: `arm.${arm.arm_slug}.failure_rate`, label: `${arm.arm_name} failure rate` },
    { value: `arm.${arm.arm_slug}.cost_usd.mean`, label: `${arm.arm_name} mean cost` },
    { value: `arm.${arm.arm_slug}.duration_ms.p95`, label: `${arm.arm_name} p95 duration` },
    ...arm.scorers.map((scorer) => ({
      value: `arm.${arm.arm_slug}.score.${scorer.scorer_id}`,
      label: `${arm.arm_name} ${scorer.scorer_name} score`,
    })),
  ]);
}

export function scoreSummary(run: BenchmarkRun) {
  const latest = new Map<string, BenchmarkAttempt>();
  for (const attempt of run.attempts ?? []) {
    const key = `${attempt.trial_id}:${attempt.repeat_index}`;
    const previous = latest.get(key);
    if (!previous || attempt.retry_index > previous.retry_index) latest.set(key, attempt);
  }
  const currentIds = new Set([...latest.values()].map((attempt) => attempt.id));
  const scored = (run.scores ?? []).filter(
    (score) => currentIds.has(score.attempt_id) && score.status === "scored" && score.score !== null,
  );
  if (!scored.length) return null;
  return scored.reduce((sum, score) => sum + Number(score.score), 0) / scored.length;
}
