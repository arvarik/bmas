/**
 * Shared types and small formatters for the evaluation operations
 * screens: studies, judge anchor sets, the resource ledger, dataset
 * version records, score records, attempt evidence, and replay bundles.
 *
 * Every type mirrors one daemon record shape as the evaluation API
 * serves it. The formatters stay pure so the screens and the tests
 * share them.
 */

export interface Money {
  currency: string;
  amount_nanos: number;
}

const NANOS_PER_UNIT = 1e9;

/** "0.4 USD" from a Money value; "Unavailable" when absent. */
export function moneyText(money: Money | null | undefined): string {
  if (!money || typeof money.amount_nanos !== "number") return "Unavailable";
  const units = money.amount_nanos / NANOS_PER_UNIT;
  const text = units.toFixed(6).replace(/\.?0+$/, "");
  return `${text === "" || text === "-" ? "0" : text} ${money.currency}`;
}

/** Parse decimal text such as "0.40" into nanos without float drift. */
export function nanosFromText(text: string): number | null {
  const trimmed = text.trim();
  if (!/^\d+(\.\d{1,9})?$/.test(trimmed)) return null;
  const [whole, fraction = ""] = trimmed.split(".");
  return Number(whole) * NANOS_PER_UNIT + Number(fraction.padEnd(9, "0"));
}

export function shortId(id: string | null | undefined): string {
  if (!id) return "";
  return id.length > 12 ? id.slice(-12) : id;
}

export function isoNow(now: Date = new Date()): string {
  return now.toISOString().replace(/\.\d{3}Z$/, "Z");
}

export function percentText(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "Unavailable";
  return `${(value * 100).toFixed(digits)}%`;
}

// ── Studies ───────────────────────────────────────────────────────────

export interface StudyCheck {
  check: string;
  passed: boolean;
  detail?: string | null;
}

export interface StudyVerdict {
  ready: boolean;
  blocking: string[];
  checks: StudyCheck[];
  stage?: string;
}

export interface StudyArm {
  slug: string;
  treatment: unknown;
  configuration_digest: string;
  configuration: Record<string, unknown>;
}

export interface StudyRecord {
  study_type: string;
  name: string;
  arms: StudyArm[];
  expansion_rule: { study_type: string; treatment: Record<string, unknown> };
  invariants: Record<string, unknown>;
  estimand: Record<string, unknown>;
  gates: { comparison_family: Record<string, unknown>; predeclared: boolean };
  sample_plan: { cases: number; repetitions: number; arms: number; attempts: number; families: number };
  estimates: { attempts: number; cost: Money; pricing_basis: string; duration_seconds: number; max_concurrency: number };
  treatment_paths: string[];
  study_digest: string;
  run_plan_id?: string;
  test_revision_id?: string;
  authored_at?: string;
}

export interface StoredStudy {
  study_id: string;
  study_type: string;
  run_plan_id: string;
  test_revision_id: string;
  record_checksum: string;
  created_at: string;
  record: StudyRecord;
}

export interface AuthoredStudy extends StudyRecord {
  published: boolean;
  study_id?: string;
  test_id?: string;
  test_revision_id?: string;
  revision?: number;
  run_plan_id?: string;
}

export interface RunStudy {
  run_id: string;
  study_id: string | null;
  plan_id: string | null;
  study: StudyRecord | null;
  verdict: StudyVerdict | null;
}

// ── Judge anchor sets and calibration ─────────────────────────────────

export interface LabelItem {
  item_id: string;
  label: string;
  input?: string;
  expected_output?: string;
  candidate?: string;
}

export interface AnchorSetRecord {
  anchor_id: string;
  judge: { judge_id: string; version: string; model: string; prompt_digest: string };
  scorer: { scorer_id: string; version: string };
  label_set: { dataset_id: string; version: string; items: LabelItem[] };
  candidate_models: string[];
  schedule: { interval_days: number; next_due_at: string; created_at: string };
  threshold: number;
  drift_tolerance: number;
  state: string;
}

export interface StoredAnchorSet {
  id: string;
  judge_id: string;
  judge_version: string;
  state: "active" | "retired";
  next_due_at: string;
  last_calibrated_at: string | null;
  created_at: string;
  record: AnchorSetRecord;
  due: boolean;
}

export interface CalibrationRecord {
  calibration_id: string;
  judge: { judge_id: string; version: string; model: string; prompt_digest: string };
  scorer: { scorer_id: string; version: string };
  dataset: { dataset_id: string; version: string; label_digest: string; item_count: number };
  agreement: { raw: number; kappa: number | null; kappa_defined: boolean; interval?: { low: number; high: number; method?: string } | null };
  disagreement: { count: number; item_ids: string[] };
  invalid_output: { count: number; rate: number };
  abstention: { count: number; rate: number };
  drift: { previous_version: string | null; raw_agreement_delta: number | null; exceeds_policy: boolean };
  state: "current" | "failed" | string;
  threshold: number;
  calibrated_at: string;
}

export interface CalibrationOutcome {
  anchor_id: string;
  calibration_id: string;
  state: string;
  raw_agreement: number;
  next_due_at: string;
  judge_outputs: Record<string, string>;
}

// ── Resource ledger ───────────────────────────────────────────────────

export type ChargeState = "estimated" | "confirmed" | "unknown" | "not_billable";

export interface LedgerEntry {
  entry_id: string;
  resource_class: string;
  provider: string;
  service: string;
  region: string;
  quantity: { value: number; unit: string };
  pricing_version: string;
  estimate?: { value: Money; method: string; estimated_at: string };
  actual?: { value: Money; evidence: { provider_text: string; source: string; invoice_reference?: string }; charged_at: string };
  charge_state: ChargeState;
  not_billable_evidence?: string;
  references: { run_id: string; attempt_id?: string; activation_id?: string; scorer_id?: string; import_id?: string; retry_of?: string };
  reservation_id: string | null;
  reconciliation_id: string | null;
  estimate_entry_id: string | null;
  recorded_at: string;
}

export interface ClassTotals {
  estimate: Money;
  actual: Money;
  entries: number;
}

export interface LedgerSummary {
  currency: string;
  estimate_total: Money;
  actual_total: Money;
  estimate_error_total: Money;
  entries_with_both: number;
  unknown_entry_ids: string[];
  not_billable_entry_ids: string[];
  per_class: Record<string, ClassTotals>;
  no_use_classes: string[];
}

export interface CostRuleOutcome {
  metric: string;
  operator: string;
  limit: Money;
  observed: Money;
  status: "passed" | "failed" | "failed_unknown" | string;
}

export interface ReconciliationRecord {
  reconciliation_version: number;
  reason: string;
  reconciled_at: string;
  currency: string;
  summary: LedgerSummary;
  reservations: unknown[];
  unmatched_reservation_references: string[];
  cost_rules: CostRuleOutcome[];
  cost_per_success: Money | null;
  unconditional_successes: number | null;
  supersedes_reconciliation: string | null;
  entry_ids: string[];
}

export interface StoredReconciliation {
  id: string;
  run_id: string;
  settlement_version: number;
  record_checksum: string;
  created_at: string;
  record: ReconciliationRecord;
}

export interface LateChargeOutcome {
  entry_id: string;
  reconciliation_id: string;
  reconciliation_version: number;
  cost_rule_changed: boolean;
  superseded_gates: number;
  analysis_recompute_required: boolean;
  affected_analysis_snapshot_ids: string[];
  recomputed_analysis_snapshots: string[];
  recompute_failures: unknown[];
}

// ── Dataset version records ───────────────────────────────────────────

export interface DatasetVersionRecord {
  version_id: string;
  parent_version_id: string | null;
  canonical_schema_version: string;
  source_lineage: string[];
  trust_inputs: Array<{ level?: string; policy_version?: string }>;
  effective_restrictions: Array<{ name: string; behavior: string }>;
  policy_digest: string;
  case_manifest_digest: string;
  transformation_recipe_digest: string;
  split_manifest: Record<string, string[]>;
  asset_digests: string[];
  content_digest: string;
  validation_report_digest: string;
  contamination_record_digest: string;
  attribution_bundle_digest: string;
}

export interface StoredDatasetVersionRecord {
  id: string;
  dataset_id: string;
  parent_version_id: string | null;
  content_digest: string;
  policy_digest: string;
  record_checksum: string;
  created_at: string;
  record: DatasetVersionRecord;
}

// ── Score records and attempt evidence ────────────────────────────────

export type SandboxBoundary = "trusted_service" | "wasi_component" | "native_microvm";

export interface ScoreSandbox {
  boundary: SandboxBoundary;
  policy_digest: string;
  runtime_digest: string;
  component_digest?: string;
  wit_digest?: string;
  compiler_digest?: string;
  dependency_lock_digest?: string;
  output_schema_digest?: string;
  terminal_class?: string;
  replay_eligible?: boolean;
  fuel_used?: number;
}

export interface ScoreRecord {
  score_id: string;
  scorer: { scorer_id: string; version: string; plugin_type?: string; configuration_digest?: string };
  evidence_references: Record<string, unknown>;
  dimensions: Array<{ name: string; value: number | null; passed?: boolean | null }>;
  explanation: string;
  status: "scored" | "error" | "excluded";
  error: string | null;
  sandbox?: ScoreSandbox;
  judge?: { model?: string; usage?: Record<string, unknown> };
  calibration_version?: string;
}

export interface StoredScoreRecord {
  id: string;
  attempt_id: string;
  scorer_version_id: string;
  status: string;
  record_checksum: string;
  created_at: string;
  record: ScoreRecord;
}

export interface RedactionReport {
  secret: string[];
  sensitive: string[];
  prohibited: string[];
  detectors: Record<string, string>;
  policy_digest: string;
}

export interface EvidenceRecord {
  attempt_id: string;
  run_manifest_digest: string;
  runtime_specification_digest: string;
  case_reference: { case_id: string; asset_ids: string[] };
  trace_digest: string | null;
  final_output_digest: string | null;
  final_state_digest?: string;
  board_state_reference?: string;
  tool_calls_digest?: string;
  verification_decisions_digest?: string;
  artifacts: string[];
  resources: Record<string, unknown>;
  completeness: { level: string; unavailable_sections: string[] };
  recovery_events?: unknown[];
  seed_evidence: Record<string, unknown>;
  redaction_policy_digest: string;
  redaction_report?: RedactionReport;
  failure_classification: string | null;
  versions: Record<string, string>;
  ledger_references: Record<string, unknown>;
}

export interface EvidenceEnvelope {
  source: "current" | "legacy";
  record: EvidenceRecord;
}

export interface EvidenceSection {
  redacted: boolean;
  reason?: string | null;
  value?: unknown;
}

// ── Replay bundles ────────────────────────────────────────────────────

export interface BundleMember {
  path: string;
  class: string;
  size_bytes: number;
  digest: string;
}

export interface BundleManifest {
  format: string;
  format_version: string | number;
  policy: string;
  redaction_policy_digest: string;
  members: BundleMember[];
  claims: Record<string, string>;
  [key: string]: unknown;
}

export interface ReplayExport {
  manifest: BundleManifest;
  manifest_digest: string;
  bundle_digest: string;
  member_count: number;
  archive_base64: string;
}

export interface ExecutionRepeatRequirements {
  import_id?: string;
  requires: string[];
  started: boolean;
  reason?: string;
}

export interface ReplayImport {
  import_id: string;
  ingestion_state: string;
  quarantined_members: string[];
  stripped_fields: string[];
  readable_members: string[];
  replay_approved: boolean;
  execution_repeat: string | ExecutionRepeatRequirements;
  replay?: {
    analysis_replayable: boolean;
    claim: string;
    results_digest: string;
    expected_results_digest: string;
    execution_repeat: string;
  };
}

/** Read the daemon's error text from a proxied JSON failure body. */
export function errorText(data: { error?: string; detail?: unknown } | null | undefined, fallback: string): string {
  if (!data) return fallback;
  if (typeof data.error === "string" && data.error) return data.error;
  if (typeof data.detail === "string" && data.detail) return data.detail;
  if (data.detail && typeof data.detail === "object" && "message" in data.detail) {
    const message = (data.detail as { message?: unknown }).message;
    if (typeof message === "string") return message;
  }
  return fallback;
}

/** "source_pinned" becomes "Source pinned"; a non-string value renders as text. */
export function statusWords(value: unknown): string {
  if (value === null || value === undefined || value === "") return "";
  const text = typeof value === "string" ? value : typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.replaceAll("_", " ").replace(/^./, (letter) => letter.toUpperCase());
}
