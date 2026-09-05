/**
 * Freezing an analysis snapshot from the browser.
 *
 * The form derives the families from the run's attempts, offers the
 * arms as the comparison ends, and lists the published metric
 * definitions the snapshot resolves. The request matches the daemon's
 * freeze input: the families, the scorer, the master seed, the
 * planned repetitions, the resample count, the metric ids, and one
 * predeclared comparison family.
 */
import type { BenchmarkAttempt } from "@/lib/benchmarks";

export interface FreezeForm {
  scorer_id: string;
  master_seed: number;
  planned_repetitions: number;
  resample_count: number;
  confidence_level: number;
  binary_reduction: string;
  metric_ids: string[];
  family_id: string;
  comparison_id: string;
  baseline_arm: string;
  candidate_arm: string;
  non_inferiority_margin: number;
}

export interface ArmOption {
  arm_id: string;
  arm_name: string;
}

/** Families from the attempts' subjects; every case joins one family. */
export function familiesFromAttempts(attempts: BenchmarkAttempt[]): Record<string, string[]> {
  const families: Record<string, Set<string>> = {};
  for (const attempt of attempts) {
    const caseId = attempt.dataset_item_id ?? attempt.item_key;
    if (!caseId) continue;
    const family = attempt.subject?.trim() || "all";
    families[family] = families[family] ?? new Set<string>();
    families[family].add(caseId);
  }
  return Object.fromEntries(Object.entries(families).sort(([left], [right]) => left.localeCompare(right)).map(([name, ids]) => [name, [...ids].sort()]));
}

export function armOptions(attempts: BenchmarkAttempt[]): ArmOption[] {
  const seen = new Map<string, ArmOption>();
  for (const attempt of attempts) {
    const armId = attempt.arm_id ?? attempt.arm_name;
    if (!armId || seen.has(armId)) continue;
    seen.set(armId, { arm_id: armId, arm_name: attempt.arm_name });
  }
  return [...seen.values()];
}

export function defaultFreezeForm(attempts: BenchmarkAttempt[], scorerId: string, seed = 7): FreezeForm {
  const arms = armOptions(attempts);
  const repetitions = attempts.reduce((highest, attempt) => Math.max(highest, (attempt.repeat_index ?? 0) + 1), 1);
  return {
    scorer_id: scorerId,
    master_seed: seed,
    planned_repetitions: repetitions,
    resample_count: 999,
    confidence_level: 0.95,
    binary_reduction: "strict_majority",
    metric_ids: [],
    family_id: "browser",
    comparison_id: "a-vs-b",
    baseline_arm: arms[0]?.arm_id ?? "",
    candidate_arm: arms[1]?.arm_id ?? arms[0]?.arm_id ?? "",
    non_inferiority_margin: 0.05,
  };
}

const IDENTIFIER_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,199}$/;
const MAX_RESAMPLES = 100000;
const MAX_REPETITIONS = 1000;

export function freezeFormErrors(form: FreezeForm, attempts: BenchmarkAttempt[]): string[] {
  const errors: string[] = [];
  if (!IDENTIFIER_PATTERN.test(form.scorer_id)) errors.push("Select the scorer the estimand reads.");
  if (Object.keys(familiesFromAttempts(attempts)).length === 0) errors.push("The run has no attempts to build families from.");
  if (!(Number.isInteger(form.master_seed) && form.master_seed >= 0)) errors.push("The master seed is a whole number of zero or more.");
  if (!(Number.isInteger(form.planned_repetitions) && form.planned_repetitions >= 1 && form.planned_repetitions <= MAX_REPETITIONS)) errors.push("Planned repetitions is 1 to 1000.");
  if (!(Number.isInteger(form.resample_count) && form.resample_count >= 1 && form.resample_count <= MAX_RESAMPLES)) errors.push("Resamples is 1 to 100000.");
  if (!(form.confidence_level > 0 && form.confidence_level < 1)) errors.push("The confidence level is between 0 and 1.");
  if (!IDENTIFIER_PATTERN.test(form.family_id)) errors.push("The comparison family id is required.");
  if (!IDENTIFIER_PATTERN.test(form.comparison_id)) errors.push("The comparison id is required.");
  if (!form.baseline_arm || !form.candidate_arm) errors.push("Select the baseline arm and the candidate arm.");
  if (!(form.non_inferiority_margin >= 0)) errors.push("The non-inferiority margin is zero or more.");
  return errors;
}

/** The freeze request the daemon's analysis route accepts. */
export function buildFreezeRequest(form: FreezeForm, attempts: BenchmarkAttempt[]) {
  return {
    families: familiesFromAttempts(attempts),
    scorer_id: form.scorer_id,
    master_seed: form.master_seed,
    planned_repetitions: form.planned_repetitions,
    resample_count: form.resample_count,
    confidence_level: form.confidence_level,
    binary_reduction: form.binary_reduction,
    metric_ids: [...form.metric_ids],
    comparison_family: {
      family_id: form.family_id,
      comparisons: [{
        comparison_id: form.comparison_id,
        baseline_arm: form.baseline_arm,
        candidate_arm: form.candidate_arm,
        non_inferiority_margin: form.non_inferiority_margin,
      }],
    },
  };
}
