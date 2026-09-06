/**
 * Judge anchor sets and their calibration schedule.
 *
 * An anchor set pins one judge version, one scorer version, and one
 * labelled item set. The schedule recalibrates the judge every
 * interval, and each calibration records raw agreement, kappa when
 * defined, the Wilson interval, drift against the previous version,
 * and the abstention and invalid-output rates. The 2026 guidance for
 * judge monitoring tracks kappa over time and alerts on drift, so the
 * screen shows the delta and the policy outcome beside the value.
 */
import { percentText, statusWords, type CalibrationRecord, type LabelItem, type StoredAnchorSet } from "@/lib/evaluation-operations";

export interface AnchorSetForm {
  anchor_id: string;
  judge_id: string;
  judge_version: string;
  judge_model: string;
  prompt_digest: string;
  scorer_id: string;
  scorer_version: string;
  dataset_id: string;
  dataset_version: string;
  dataset_version_id: string;
  items: string;
  candidate_models: string;
  interval_days: number;
  threshold: number;
  drift_tolerance: number;
}

const IDENTIFIER_PATTERN = /^[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,199}$/;
const DIGEST_PATTERN = /^[a-f0-9]{64}$/;
const DAY_MS = 24 * 60 * 60 * 1000;

export function defaultAnchorSetForm(): AnchorSetForm {
  return {
    anchor_id: "",
    judge_id: "",
    judge_version: "1",
    judge_model: "",
    prompt_digest: "0".repeat(64),
    scorer_id: "",
    scorer_version: "1",
    dataset_id: "",
    dataset_version: "1",
    dataset_version_id: "",
    items: "",
    candidate_models: "",
    interval_days: 7,
    threshold: 0.7,
    drift_tolerance: 0.1,
  };
}

/**
 * One anchor item per line: "item_id, label" or "item_id, label, candidate".
 * A third column pins the candidate answer the label judges; the input
 * and the reference answer resolve from the dataset item with the same id.
 */
export function parseLabelItems(text: string): LabelItem[] {
  const items: LabelItem[] = [];
  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    const parts = trimmed.split(/[,\t]/).map((part) => part.trim());
    if (parts.length < 2 || !parts[0] || !parts[1]) continue;
    const item: LabelItem = { item_id: parts[0], label: parts[1] };
    const candidate = parts.slice(2).join(", ").trim();
    if (candidate) item.candidate = candidate;
    items.push(item);
  }
  return items;
}

export function anchorSetFormErrors(form: AnchorSetForm): string[] {
  const errors: string[] = [];
  for (const [label, value] of [["anchor id", form.anchor_id], ["judge id", form.judge_id], ["scorer id", form.scorer_id]] as const) {
    if (!IDENTIFIER_PATTERN.test(value)) errors.push(`The ${label} uses letters, digits, and - _ . : @ / only.`);
  }
  if (!form.judge_version.trim()) errors.push("The judge version is required.");
  if (!form.judge_model.trim()) errors.push("The judge model is required.");
  if (!DIGEST_PATTERN.test(form.prompt_digest)) errors.push("The prompt digest is 64 hex characters.");
  if (!form.scorer_version.trim()) errors.push("The scorer version is required.");
  if (!form.dataset_id.trim()) errors.push("The label set names its dataset.");
  if (!form.dataset_version.trim()) errors.push("The label set names its dataset version.");
  const items = parseLabelItems(form.items);
  if (items.length < 2) errors.push("List at least two labelled items, one per line as id, label.");
  const ids = new Set(items.map((item) => item.item_id));
  if (ids.size !== items.length) errors.push("Every anchor item id is unique.");
  if (!(Number.isInteger(form.interval_days) && form.interval_days >= 1 && form.interval_days <= 365)) errors.push("The interval is 1 to 365 days.");
  if (!(form.threshold >= 0 && form.threshold <= 1)) errors.push("The agreement threshold is between 0 and 1.");
  if (!(form.drift_tolerance >= 0 && form.drift_tolerance <= 1)) errors.push("The drift tolerance is between 0 and 1.");
  return errors;
}

/** The anchor set registration the daemon validates. */
export function buildAnchorSetRequest(form: AnchorSetForm, registeredAt: string) {
  return {
    anchor_id: form.anchor_id.trim(),
    judge_id: form.judge_id.trim(),
    judge_version: form.judge_version.trim(),
    judge_model: form.judge_model.trim(),
    prompt_digest: form.prompt_digest.trim(),
    scorer_id: form.scorer_id.trim(),
    scorer_version: form.scorer_version.trim(),
    label_set: {
      dataset_id: form.dataset_id.trim(),
      version: form.dataset_version.trim(),
      ...(form.dataset_version_id.trim() ? { dataset_version_id: form.dataset_version_id.trim() } : {}),
      items: parseLabelItems(form.items),
    },
    candidate_models: form.candidate_models.split(/[,\n]/).map((entry) => entry.trim()).filter(Boolean),
    interval_days: form.interval_days,
    threshold: form.threshold,
    drift_tolerance: form.drift_tolerance,
    registered_at: registeredAt,
  };
}

export interface ScheduleStatus {
  label: string;
  tone: "passed" | "failed" | "cancelled" | "provisional";
  dueAt: string;
  daysUntilDue: number | null;
}

/** Where one anchor set stands against its schedule at one moment. */
export function scheduleStatus(anchorSet: Pick<StoredAnchorSet, "state" | "next_due_at" | "due">, now: Date): ScheduleStatus {
  if (anchorSet.state === "retired") return { label: "Retired", tone: "cancelled", dueAt: anchorSet.next_due_at, daysUntilDue: null };
  const dueMs = Date.parse(anchorSet.next_due_at);
  if (Number.isNaN(dueMs)) return { label: "Schedule unknown", tone: "provisional", dueAt: anchorSet.next_due_at, daysUntilDue: null };
  const days = Math.ceil((dueMs - now.getTime()) / DAY_MS);
  if (anchorSet.due || days <= 0) {
    const overdue = Math.max(0, -days);
    return { label: overdue > 0 ? `Overdue by ${overdue} day${overdue === 1 ? "" : "s"}` : "Due now", tone: "failed", dueAt: anchorSet.next_due_at, daysUntilDue: days };
  }
  return { label: `Due in ${days} day${days === 1 ? "" : "s"}`, tone: "passed", dueAt: anchorSet.next_due_at, daysUntilDue: days };
}

export interface AgreementSummary {
  available: boolean;
  state: string;
  tone: "passed" | "failed" | "indeterminate";
  raw: string;
  kappa: string;
  interval: string;
  threshold: string;
  driftDelta: string;
  exceedsPolicy: boolean;
  abstention: string;
  invalidOutput: string;
  disagreements: string;
  calibratedAt: string | null;
  itemCount: number;
}

function signedPoints(delta: number): string {
  const points = delta * 100;
  return `${points >= 0 ? "+" : ""}${points.toFixed(1)} pp`;
}

/** The latest calibration of one judge version, or the reason none shows. */
export function agreementSummary(calibration: CalibrationRecord | null | undefined): AgreementSummary {
  if (!calibration) {
    return {
      available: false, state: "none", tone: "indeterminate", raw: "Not calibrated", kappa: "Not calibrated", interval: "",
      threshold: "", driftDelta: "No calibration yet", exceedsPolicy: false, abstention: "", invalidOutput: "", disagreements: "",
      calibratedAt: null, itemCount: 0,
    };
  }
  const agreement = calibration.agreement;
  const interval = agreement.interval;
  return {
    available: true,
    state: calibration.state,
    tone: calibration.state === "current" ? "passed" : calibration.state === "failed" ? "failed" : "indeterminate",
    raw: percentText(agreement.raw),
    kappa: agreement.kappa_defined && agreement.kappa !== null ? agreement.kappa.toFixed(2) : "Undefined (one label class)",
    interval: interval ? `${percentText(interval.low)} to ${percentText(interval.high)}` : "",
    threshold: percentText(calibration.threshold, 0),
    driftDelta: calibration.drift.previous_version === null || calibration.drift.raw_agreement_delta === null
      ? "No previous version"
      : `${signedPoints(calibration.drift.raw_agreement_delta)} against ${calibration.drift.previous_version}`,
    exceedsPolicy: calibration.drift.exceeds_policy,
    abstention: `${percentText(calibration.abstention.rate)} (${calibration.abstention.count})`,
    invalidOutput: `${percentText(calibration.invalid_output.rate)} (${calibration.invalid_output.count})`,
    disagreements: `${calibration.disagreement.count} of ${calibration.dataset.item_count}`,
    calibratedAt: calibration.calibrated_at,
    itemCount: calibration.dataset.item_count,
  };
}

export function calibrationStateLabel(state: string): string {
  return statusWords(state);
}
