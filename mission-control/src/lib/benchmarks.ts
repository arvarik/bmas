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

export function runProgress(run: Pick<BenchmarkRun, "completed_attempts" | "total_attempts">) {
  if (run.total_attempts <= 0) return 0;
  return Math.min(100, Math.round((run.completed_attempts / run.total_attempts) * 100));
}

export function statusLabel(status: string) {
  return status.replaceAll("_", " ").replace(/^./, (value) => value.toUpperCase());
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
