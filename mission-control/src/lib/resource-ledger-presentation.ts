/**
 * The per-run resource ledger: totals per resource class, the entries
 * that stay unknown or not billable, the reconciliation versions, and
 * the late charge form.
 *
 * The ledger never replaces an estimate with an actual charge. A late
 * charge stores as a new confirmed entry that references its estimate,
 * opens the next reconciliation version, and, when it changes a cost
 * rule outcome, supersedes every stored gate and recomputes every
 * current analysis snapshot. The screen keeps every version visible,
 * following the versioned reconciliation dashboards of 2026 where the
 * unmatched and unmodeled charges stay first-class rows.
 */
import {
  moneyText,
  nanosFromText,
  statusWords,
  type CostRuleOutcome,
  type LedgerEntry,
  type LedgerSummary,
  type Money,
  type StoredReconciliation,
} from "@/lib/evaluation-operations";

export const RESOURCE_CLASSES = [
  "runtime", "control_plane", "scorer", "judge", "environment",
  "external_tool", "import", "transformation", "storage", "human_review",
] as const;

export interface ClassRow {
  resourceClass: string;
  label: string;
  entries: number;
  estimate: Money;
  actual: Money;
  estimateText: string;
  actualText: string;
  differenceText: string;
}

function differenceText(estimate: Money, actual: Money): string {
  if (estimate.currency !== actual.currency) return "Mixed currencies";
  const delta = actual.amount_nanos - estimate.amount_nanos;
  const text = moneyText({ currency: actual.currency, amount_nanos: Math.abs(delta) });
  if (delta === 0) return "Matches estimate";
  return `${delta > 0 ? "+" : "-"}${text}`;
}

/** One row per resource class with use, sorted by actual then estimate. */
export function classRows(summary: LedgerSummary | null | undefined): ClassRow[] {
  if (!summary) return [];
  return Object.entries(summary.per_class)
    .map(([resourceClass, totals]) => ({
      resourceClass,
      label: statusWords(resourceClass),
      entries: totals.entries,
      estimate: totals.estimate,
      actual: totals.actual,
      estimateText: moneyText(totals.estimate),
      actualText: moneyText(totals.actual),
      differenceText: differenceText(totals.estimate, totals.actual),
    }))
    .sort((left, right) => right.actual.amount_nanos - left.actual.amount_nanos || right.estimate.amount_nanos - left.estimate.amount_nanos || left.resourceClass.localeCompare(right.resourceClass));
}

export interface FlaggedEntries {
  unknown: LedgerEntry[];
  notBillable: LedgerEntry[];
}

/** The entries with no usable amount: unknown prices and not-billable use. */
export function flaggedEntries(entries: LedgerEntry[], summary: LedgerSummary | null | undefined): FlaggedEntries {
  const unknownIds = new Set(summary?.unknown_entry_ids ?? entries.filter((entry) => entry.charge_state === "unknown").map((entry) => entry.entry_id));
  const notBillableIds = new Set(summary?.not_billable_entry_ids ?? entries.filter((entry) => entry.charge_state === "not_billable").map((entry) => entry.entry_id));
  return {
    unknown: entries.filter((entry) => unknownIds.has(entry.entry_id)),
    notBillable: entries.filter((entry) => notBillableIds.has(entry.entry_id)),
  };
}

export interface EntryRow {
  entryId: string;
  resourceClass: string;
  source: string;
  quantity: string;
  estimate: string;
  actual: string;
  chargeState: string;
  tone: "passed" | "failed" | "provisional" | "paused";
  reference: string;
  recordedAt: string;
  estimateEntryId: string | null;
}

const CHARGE_TONES: Record<string, EntryRow["tone"]> = {
  confirmed: "passed",
  estimated: "provisional",
  unknown: "failed",
  not_billable: "paused",
};

/** Every entry as one table row, newest last. */
export function entryRows(entries: LedgerEntry[]): EntryRow[] {
  return [...entries]
    .sort((left, right) => left.recorded_at.localeCompare(right.recorded_at) || left.entry_id.localeCompare(right.entry_id))
    .map((entry) => ({
      entryId: entry.entry_id,
      resourceClass: statusWords(entry.resource_class),
      source: `${entry.provider} · ${entry.service}`,
      quantity: `${entry.quantity.value.toLocaleString()} ${entry.quantity.unit}`,
      estimate: entry.estimate ? moneyText(entry.estimate.value) : entry.charge_state === "unknown" ? "Unknown price" : "None",
      actual: entry.actual ? `${moneyText(entry.actual.value)} (${entry.actual.evidence.provider_text})` : entry.charge_state === "not_billable" ? entry.not_billable_evidence ?? "Not billable" : "None",
      chargeState: statusWords(entry.charge_state),
      tone: CHARGE_TONES[entry.charge_state] ?? "provisional",
      reference: entry.references.attempt_id ?? entry.references.scorer_id ?? entry.references.import_id ?? entry.references.activation_id ?? "run",
      recordedAt: entry.recorded_at,
      estimateEntryId: entry.estimate_entry_id,
    }));
}

export interface RuleRow {
  label: string;
  status: string;
  tone: "passed" | "failed";
  observed: string;
}

export interface ReconciliationRow {
  id: string;
  version: number;
  reason: string;
  reasonLabel: string;
  lateCharge: boolean;
  reconciledAt: string;
  estimateTotal: string;
  actualTotal: string;
  costPerSuccess: string;
  unconditionalSuccesses: number | null;
  supersedes: string | null;
  rules: RuleRow[];
  unknownEntries: number;
  unmatchedReservations: number;
}

function ruleRow(rule: CostRuleOutcome): RuleRow {
  return {
    label: `${statusWords(rule.metric)} ${rule.operator === "lte" ? "≤" : rule.operator} ${moneyText(rule.limit)}`,
    status: statusWords(rule.status),
    tone: rule.status === "passed" ? "passed" : "failed",
    observed: moneyText(rule.observed),
  };
}

/** Every stored reconciliation version, newest first. */
export function reconciliationRows(reconciliations: StoredReconciliation[]): ReconciliationRow[] {
  return [...reconciliations]
    .sort((left, right) => right.record.reconciliation_version - left.record.reconciliation_version)
    .map((stored) => {
      const record = stored.record;
      return {
        id: stored.id,
        version: record.reconciliation_version,
        reason: record.reason,
        reasonLabel: statusWords(record.reason),
        lateCharge: record.reason === "late_charge",
        reconciledAt: record.reconciled_at,
        estimateTotal: moneyText(record.summary.estimate_total),
        actualTotal: moneyText(record.summary.actual_total),
        costPerSuccess: record.cost_per_success ? moneyText(record.cost_per_success) : "Not declared",
        unconditionalSuccesses: record.unconditional_successes,
        supersedes: record.supersedes_reconciliation,
        rules: record.cost_rules.map(ruleRow),
        unknownEntries: record.summary.unknown_entry_ids.length,
        unmatchedReservations: record.unmatched_reservation_references.length,
      };
    });
}

export interface LateChargeForm {
  estimate_entry_id: string;
  resource_class: string;
  provider: string;
  service: string;
  region: string;
  quantity: number;
  unit: string;
  pricing_version: string;
  amount: string;
  provider_text: string;
  source: string;
  invoice_reference: string;
  cost_limit: string;
  unconditional_successes: string;
}

export function defaultLateChargeForm(): LateChargeForm {
  return {
    estimate_entry_id: "",
    resource_class: "runtime",
    provider: "",
    service: "",
    region: "global",
    quantity: 1,
    unit: "tokens",
    pricing_version: "invoice",
    amount: "",
    provider_text: "",
    source: "invoice",
    invoice_reference: "",
    cost_limit: "",
    unconditional_successes: "",
  };
}

/** Prefill the late charge from the estimate it confirms. */
export function lateChargeFromEstimate(form: LateChargeForm, estimate: LedgerEntry | null): LateChargeForm {
  if (!estimate) return { ...form, estimate_entry_id: "" };
  return {
    ...form,
    estimate_entry_id: estimate.entry_id,
    resource_class: estimate.resource_class,
    provider: estimate.provider,
    service: estimate.service,
    region: estimate.region,
    quantity: estimate.quantity.value,
    unit: estimate.quantity.unit,
    pricing_version: estimate.pricing_version,
  };
}

export function lateChargeFormErrors(form: LateChargeForm): string[] {
  const errors: string[] = [];
  if (!(RESOURCE_CLASSES as readonly string[]).includes(form.resource_class)) errors.push("Select a resource class.");
  for (const [label, value] of [["provider", form.provider], ["service", form.service], ["region", form.region], ["unit", form.unit], ["pricing version", form.pricing_version], ["source", form.source]] as const) {
    if (!value.trim()) errors.push(`The ${label} is required.`);
  }
  if (!(form.quantity >= 0)) errors.push("The quantity is zero or more.");
  if (nanosFromText(form.amount) === null) errors.push("The charged amount is a decimal such as 0.40.");
  if (!form.provider_text.trim()) errors.push("Keep the provider's original amount text as evidence.");
  if (form.cost_limit.trim() && nanosFromText(form.cost_limit) === null) errors.push("The cost limit is a decimal amount.");
  if (form.unconditional_successes.trim() && !/^\d+$/.test(form.unconditional_successes.trim())) errors.push("Unconditional successes is a whole number.");
  return errors;
}

function entryIdentifier(now: string): string {
  const stamp = now.replace(/[^0-9]/g, "").slice(0, 14);
  const random = Math.random().toString(36).slice(2, 8);
  return `late-charge-${stamp}-${random}`;
}

/** The confirmed ledger entry one late charge stores. */
export function buildLateChargeEntry(form: LateChargeForm, runId: string, currency: string, now: string, entryId = entryIdentifier(now)) {
  const amount: Money = { currency, amount_nanos: nanosFromText(form.amount) ?? 0 };
  const actual: { value: Money; evidence: { provider_text: string; source: string; invoice_reference?: string }; charged_at: string } = {
    value: amount,
    evidence: { provider_text: form.provider_text.trim(), source: form.source.trim() },
    charged_at: now,
  };
  if (form.invoice_reference.trim()) actual.evidence.invoice_reference = form.invoice_reference.trim();
  return {
    schema_id: "resource-ledger-entry",
    schema_version: 2,
    entry_id: entryId,
    resource_class: form.resource_class,
    provider: form.provider.trim(),
    service: form.service.trim(),
    region: form.region.trim(),
    quantity: { value: form.quantity, unit: form.unit.trim() },
    pricing_version: form.pricing_version.trim(),
    actual,
    charge_state: "confirmed",
    references: { run_id: runId },
    reservation_id: null,
    reconciliation_id: null,
    estimate_entry_id: form.estimate_entry_id.trim() || null,
    recorded_at: now,
  };
}

/** The reconciliation request body, with or without a late charge. */
export function buildReconciliationRequest(
  runId: string,
  currency: string,
  now: string,
  options: { lateCharge?: LateChargeForm; costLimit?: string; unconditionalSuccesses?: string },
) {
  const rules: Array<{ metric: string; operator: string; value: Money }> = [];
  const limit = options.costLimit?.trim() ? nanosFromText(options.costLimit) : null;
  if (limit !== null && limit !== undefined) rules.push({ metric: "actual_total", operator: "lte", value: { currency, amount_nanos: limit } });
  const successes = options.unconditionalSuccesses?.trim();
  return {
    currency,
    cost_rules: rules,
    ...(successes ? { unconditional_successes: Number(successes) } : {}),
    ...(options.lateCharge ? { late_charge: buildLateChargeEntry(options.lateCharge, runId, currency, now) } : {}),
    reconciled_at: now,
  };
}
