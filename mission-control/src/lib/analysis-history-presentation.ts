/**
 * Presentation helpers for the analysis history of one run.
 *
 * The daemon lists every stored snapshot with the snapshot that
 * superseded it, when one did. These helpers order the snapshots,
 * link each superseded snapshot to its successor, and lay two
 * overviews side by side so an operator reads what a recomputation
 * changed without recomputing anything in the browser.
 */

export interface AnalysisSnapshotSummary {
  id: string;
  record_checksum: string;
  created_at: string;
  superseded_by: string | null;
  supersession_reason: string | null;
  current: boolean;
}

export interface SnapshotChainEntry {
  snapshot: AnalysisSnapshotSummary;
  replacedBy: AnalysisSnapshotSummary | null;
  replaces: AnalysisSnapshotSummary | null;
  position: number;
}

export interface SnapshotChain {
  entries: SnapshotChainEntry[];
  current: AnalysisSnapshotSummary | null;
  pairs: Array<{ superseded: AnalysisSnapshotSummary; successor: AnalysisSnapshotSummary }>;
}

/** Order the snapshots oldest first and link every supersession. */
export function snapshotChain(snapshots: AnalysisSnapshotSummary[]): SnapshotChain {
  const ordered = [...snapshots].sort((left, right) =>
    left.created_at === right.created_at ? left.id.localeCompare(right.id) : left.created_at < right.created_at ? -1 : 1,
  );
  const byId = new Map(ordered.map((snapshot) => [snapshot.id, snapshot]));
  const replacesById = new Map<string, AnalysisSnapshotSummary>();
  for (const snapshot of ordered) {
    if (snapshot.superseded_by) replacesById.set(snapshot.superseded_by, snapshot);
  }
  const entries = ordered.map((snapshot, position) => ({
    snapshot,
    replacedBy: snapshot.superseded_by ? byId.get(snapshot.superseded_by) ?? null : null,
    replaces: replacesById.get(snapshot.id) ?? null,
    position,
  }));
  const currents = ordered.filter((snapshot) => snapshot.current);
  const current = currents.length ? currents[currents.length - 1] : null;
  const pairs = entries
    .filter((entry) => entry.replacedBy !== null)
    .map((entry) => ({ superseded: entry.snapshot, successor: entry.replacedBy as AnalysisSnapshotSummary }));
  return { entries, current, pairs };
}

export interface PrimaryMetricRow {
  comparison_id: string;
  estimate: number | null;
  interval_low: number | null;
  interval_high: number | null;
  interval_status: string;
  unit: string;
  method: string;
  p_value_adjusted: number | null;
  multiplicity_family?: string;
  gate: string;
  primary_valid: boolean;
}

export interface AnalysisOverview {
  sections: Array<{ view: string; rows?: PrimaryMetricRow[]; [key: string]: unknown }>;
  estimand: string;
  replay: { claim: string; [key: string]: unknown };
  resources: {
    available: boolean;
    statement?: string;
    currency?: string;
    actual_total?: { currency: string; amount_nanos: number };
    cost_per_success?: { currency: string; amount_nanos: number } | null;
    unconditional_successes?: number;
  };
}

/** The primary metric rows of one overview, keyed by comparison. */
export function primaryMetricRows(overview: AnalysisOverview | null | undefined): PrimaryMetricRow[] {
  if (!overview) return [];
  const section = overview.sections.find((entry) => entry.view === "primary_metric_with_uncertainty");
  return (section?.rows ?? []) as PrimaryMetricRow[];
}

export interface SideBySideRow {
  key: string;
  label: string;
  left: string;
  right: string;
  changed: boolean;
}

function money(value: { currency: string; amount_nanos: number } | null | undefined): string {
  if (!value) return "Unavailable";
  const units = Math.trunc(value.amount_nanos / 1e9);
  const nanos = Math.abs(value.amount_nanos % 1e9);
  const fraction = String(nanos).padStart(9, "0").replace(/0+$/, "");
  return `${fraction ? `${units}.${fraction}` : String(units)} ${value.currency}`;
}

function points(value: number | null): string {
  if (value === null || value === undefined) return "Unavailable";
  const scaled = value * 100;
  return `${scaled > 0 ? "+" : ""}${scaled.toFixed(1)} pp`;
}

function interval(row: PrimaryMetricRow): string {
  if (row.interval_status !== "estimated" && row.interval_status !== "degenerate") {
    return row.interval_status.replaceAll("_", " ");
  }
  return `${points(row.interval_low)} to ${points(row.interval_high)}`;
}

/** Lay two overviews side by side and flag every value that changed. */
export function sideBySide(
  left: AnalysisOverview | null | undefined,
  right: AnalysisOverview | null | undefined,
): SideBySideRow[] {
  const rows: SideBySideRow[] = [];
  const push = (key: string, label: string, leftValue: string, rightValue: string) => {
    rows.push({ key, label, left: leftValue, right: rightValue, changed: leftValue !== rightValue });
  };
  push("estimand", "Estimand", left?.estimand ?? "Unavailable", right?.estimand ?? "Unavailable");
  push("replay", "Replay claim", left?.replay.claim ?? "Unavailable", right?.replay.claim ?? "Unavailable");
  const leftRows = new Map(primaryMetricRows(left).map((row) => [row.comparison_id, row]));
  const rightRows = new Map(primaryMetricRows(right).map((row) => [row.comparison_id, row]));
  for (const id of new Set([...leftRows.keys(), ...rightRows.keys()])) {
    const l = leftRows.get(id);
    const r = rightRows.get(id);
    push(`${id}:estimate`, `${id} estimate`, l ? points(l.estimate) : "Absent", r ? points(r.estimate) : "Absent");
    push(`${id}:interval`, `${id} interval`, l ? interval(l) : "Absent", r ? interval(r) : "Absent");
    push(`${id}:gate`, `${id} gate`, l?.gate ?? "Absent", r?.gate ?? "Absent");
    push(
      `${id}:p`,
      `${id} Holm-adjusted p`,
      l ? (l.p_value_adjusted === null ? "Unavailable" : l.p_value_adjusted.toFixed(3)) : "Absent",
      r ? (r.p_value_adjusted === null ? "Unavailable" : r.p_value_adjusted.toFixed(3)) : "Absent",
    );
  }
  const leftResources = left?.resources;
  const rightResources = right?.resources;
  push(
    "cost",
    "Actual cost",
    leftResources?.available ? money(leftResources.actual_total) : "No resource ledger",
    rightResources?.available ? money(rightResources.actual_total) : "No resource ledger",
  );
  push(
    "cost_per_success",
    "Cost per success",
    leftResources?.available ? money(leftResources.cost_per_success) : "No resource ledger",
    rightResources?.available ? money(rightResources.cost_per_success) : "No resource ledger",
  );
  return rows;
}

export function supersessionReasonLabel(reason: string | null): string {
  if (!reason) return "";
  return reason.replaceAll("_", " ").replace(/^./, (value) => value.toUpperCase());
}
