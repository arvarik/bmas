/**
 * Study authoring: the form, its problems, the request it builds, the
 * estimate rows a preview shows, and the admission verdict summary.
 *
 * A study expands one treatment over one base configuration into arms,
 * freezes the case schedule and the seed schedule as invariants, and
 * predeclares the comparison family. Publication writes one test
 * revision and one run plan, and admission validates the study
 * conditions of every run that carries the plan.
 */
import { moneyText, nanosFromText, statusWords, type Money, type StudyCheck, type StudyRecord, type StudyVerdict } from "@/lib/evaluation-operations";

export const STUDY_TYPES = [
  "one_factor_ablation",
  "parameter_grid",
  "preset_comparison",
  "runtime_comparison",
  "model_family_comparison",
] as const;

export type StudyType = typeof STUDY_TYPES[number];
export type StudyHypothesis = "non_inferiority" | "superiority";

export interface StudyForm {
  study_type: StudyType;
  name: string;
  base_configuration: string;
  treatment_path: string;
  treatment_values: string;
  dataset_version_id: string;
  case_ids: string;
  families: string;
  base_seed: number;
  scorer_id: string;
  repetitions: number;
  master_seed: number;
  comparison_margin: number;
  per_attempt_cost: string;
  currency: string;
  seconds_per_attempt: number;
  max_concurrency: number;
  hypothesis: StudyHypothesis;
  runtime_id: string;
}

export function defaultStudyForm(): StudyForm {
  return {
    study_type: "one_factor_ablation",
    name: "",
    base_configuration: "{\n  \"classic\": {\n    \"max_rounds\": 4\n  }\n}",
    treatment_path: "classic.max_rounds",
    treatment_values: "4, 6",
    dataset_version_id: "",
    case_ids: "",
    families: "",
    base_seed: 11,
    scorer_id: "",
    repetitions: 1,
    master_seed: 11,
    comparison_margin: 0.05,
    per_attempt_cost: "0.005",
    currency: "USD",
    seconds_per_attempt: 15,
    max_concurrency: 4,
    hypothesis: "non_inferiority",
    runtime_id: "classic",
  };
}

const IDENTIFIER_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,199}$/;

export function splitList(text: string): string[] {
  return text.split(/[,\n]/).map((entry) => entry.trim()).filter(Boolean);
}

/** A treatment value keeps its JSON type: 4 stays a number, "4" stays text. */
export function treatmentValue(text: string): unknown {
  const trimmed = text.trim();
  if (/^-?\d+(\.\d+)?$/.test(trimmed) || trimmed === "true" || trimmed === "false" || trimmed === "null") {
    return JSON.parse(trimmed);
  }
  return trimmed;
}

/** Parse "family: id, id" lines; an empty text puts every case in one family. */
export function parseFamilies(text: string, caseIds: string[]): Record<string, string[]> {
  const families: Record<string, string[]> = {};
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const separator = trimmed.indexOf(":");
    if (separator <= 0) continue;
    const name = trimmed.slice(0, separator).trim();
    const members = splitList(trimmed.slice(separator + 1));
    if (name && members.length) families[name] = members;
  }
  if (Object.keys(families).length === 0 && caseIds.length) families.all = [...caseIds];
  return families;
}

function parseObject(text: string): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed as Record<string, unknown>;
    return null;
  } catch {
    return null;
  }
}

/** Every reason the form cannot become a valid study yet. */
export function studyFormErrors(form: StudyForm): string[] {
  const errors: string[] = [];
  if (!form.name.trim()) errors.push("The study needs a name.");
  if (!parseObject(form.base_configuration)) errors.push("The base configuration is one JSON object.");
  if (!form.treatment_path.trim()) errors.push("The treatment path names the configuration field the study varies.");
  if (splitList(form.treatment_values).length < 2) errors.push("The treatment needs at least two values, one per arm.");
  if (!IDENTIFIER_PATTERN.test(form.dataset_version_id)) errors.push("Select the dataset version the study runs on.");
  const caseIds = splitList(form.case_ids);
  if (caseIds.length === 0) errors.push("List at least one case id.");
  const families = parseFamilies(form.families, caseIds);
  const covered = new Set(Object.values(families).flat());
  const uncovered = caseIds.filter((caseId) => !covered.has(caseId));
  if (caseIds.length && uncovered.length) errors.push(`Every case belongs to one family; ${uncovered.length} cases have none.`);
  if (!IDENTIFIER_PATTERN.test(form.scorer_id)) errors.push("Select the scorer the estimand reads.");
  if (!(Number.isInteger(form.repetitions) && form.repetitions >= 1)) errors.push("Repetitions is a whole number of at least 1.");
  if (!(form.comparison_margin >= 0)) errors.push("The comparison margin is zero or more.");
  if (nanosFromText(form.per_attempt_cost) === null) errors.push("The cost per attempt is a decimal amount such as 0.005.");
  if (!/^[A-Z]{3}$/.test(form.currency)) errors.push("The currency is a three-letter code.");
  if (!(Number.isInteger(form.seconds_per_attempt) && form.seconds_per_attempt >= 1)) errors.push("Seconds per attempt is at least 1.");
  if (!(Number.isInteger(form.max_concurrency) && form.max_concurrency >= 1)) errors.push("Concurrency is at least 1.");
  if (!IDENTIFIER_PATTERN.test(form.runtime_id)) errors.push("The runtime id is required.");
  return errors;
}

export interface StudyRequestOptions {
  publish: boolean;
  scorerVersions?: Array<{ id: string; configuration: Record<string, unknown> }>;
  authoredAt?: string;
}

/** The study input the daemon validates, previews, or publishes. */
export function buildStudyRequest(form: StudyForm, options: StudyRequestOptions) {
  const caseIds = splitList(form.case_ids);
  const values = splitList(form.treatment_values).map(treatmentValue);
  const cost: Money = { currency: form.currency, amount_nanos: nanosFromText(form.per_attempt_cost) ?? 0 };
  return {
    study_type: form.study_type,
    name: form.name.trim(),
    base_configuration: parseObject(form.base_configuration) ?? {},
    treatment: { path: form.treatment_path.trim(), values },
    invariants: {
      dataset_version_id: form.dataset_version_id.trim(),
      case_ids: caseIds,
      seed_schedule: { base_seed: form.base_seed },
      scorers: [form.scorer_id.trim()],
      arm_order: "rotated_interleave",
      repetitions: form.repetitions,
    },
    families: parseFamilies(form.families, caseIds),
    scorer_id: form.scorer_id.trim(),
    master_seed: form.master_seed,
    comparison_margin: form.comparison_margin,
    per_attempt_cost: cost,
    seconds_per_attempt: form.seconds_per_attempt,
    max_concurrency: form.max_concurrency,
    hypothesis: form.hypothesis,
    publish: options.publish,
    runtime_id: form.runtime_id.trim(),
    scorer_versions: options.publish
      ? (options.scorerVersions ?? [{ id: form.scorer_id.trim(), configuration: {} }])
      : [],
    ...(options.authoredAt ? { authored_at: options.authoredAt } : {}),
  };
}

export function durationText(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "Unavailable";
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${(seconds / 60).toFixed(1)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

export interface EstimateRow {
  label: string;
  value: string;
}

/** The sample plan and the estimate a preview shows before publication. */
export function estimateRows(study: Pick<StudyRecord, "sample_plan" | "estimates" | "arms">): EstimateRow[] {
  const plan = study.sample_plan;
  const estimates = study.estimates;
  return [
    { label: "Arms", value: `${plan.arms} (${study.arms.map((arm) => arm.slug).join(", ")})` },
    { label: "Cases", value: String(plan.cases) },
    { label: "Families", value: String(plan.families) },
    { label: "Repetitions", value: String(plan.repetitions) },
    { label: "Attempts", value: String(plan.attempts) },
    { label: "Estimated cost", value: `${moneyText(estimates.cost)} (${statusWords(estimates.pricing_basis)})` },
    { label: "Estimated duration", value: `${durationText(estimates.duration_seconds)} at concurrency ${estimates.max_concurrency}` },
  ];
}

export interface VerdictSummary {
  ready: boolean;
  passed: StudyCheck[];
  failed: StudyCheck[];
  blocking: string[];
  title: string;
  tone: "passed" | "failed" | "indeterminate";
}

/** Group the admission checks and name the verdict for people. */
export function verdictSummary(verdict: StudyVerdict | null | undefined): VerdictSummary {
  if (!verdict) {
    return { ready: false, passed: [], failed: [], blocking: [], title: "No study conditions", tone: "indeterminate" };
  }
  const passed = verdict.checks.filter((check) => check.passed);
  const failed = verdict.checks.filter((check) => !check.passed);
  return {
    ready: verdict.ready,
    passed,
    failed,
    blocking: verdict.blocking,
    title: verdict.ready ? "Admission ready" : `Admission blocked by ${verdict.blocking.length} condition${verdict.blocking.length === 1 ? "" : "s"}`,
    tone: verdict.ready ? "passed" : "failed",
  };
}

export function checkLabel(name: string): string {
  return statusWords(name);
}
