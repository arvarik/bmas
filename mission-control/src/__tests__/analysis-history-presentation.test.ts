/**
 * The analysis history presentation orders snapshots, links every
 * supersession to its successor, and lays two overviews side by side
 * with every changed value flagged.
 */
import { describe, expect, it } from "vitest";

import {
  primaryMetricRows,
  sideBySide,
  snapshotChain,
  supersessionReasonLabel,
  type AnalysisOverview,
  type AnalysisSnapshotSummary,
} from "@/lib/analysis-history-presentation";

const snapshots: AnalysisSnapshotSummary[] = [
  { id: "snapshot-b", record_checksum: "b".repeat(64), created_at: "2026-09-02T00:00:00Z", superseded_by: null, supersession_reason: null, current: true },
  { id: "snapshot-a", record_checksum: "a".repeat(64), created_at: "2026-09-01T00:00:00Z", superseded_by: "snapshot-b", supersession_reason: "late_charge_changed_cost_rule", current: false },
];

function overview(estimate: number, gate: string, costNanos: number | null): AnalysisOverview {
  return {
    sections: [
      { view: "success_funnel" },
      {
        view: "primary_metric_with_uncertainty",
        rows: [{ comparison_id: "a-vs-b", estimate, interval_low: estimate - 0.1, interval_high: estimate + 0.1, interval_status: "estimated", unit: "case", method: "bootstrap", p_value_adjusted: 0.3, gate, primary_valid: true }],
      },
    ],
    estimand: "family-balanced-unconditional-task-success",
    replay: { claim: "analysis_replayable" },
    resources: costNanos === null
      ? { available: false, statement: "no resource ledger" }
      : { available: true, currency: "USD", actual_total: { currency: "USD", amount_nanos: costNanos }, cost_per_success: { currency: "USD", amount_nanos: costNanos / 2 }, unconditional_successes: 2 },
  };
}

describe("analysis history presentation", () => {
  it("orders snapshots oldest first and links each supersession", () => {
    const chain = snapshotChain(snapshots);
    expect(chain.entries.map((entry) => entry.snapshot.id)).toEqual(["snapshot-a", "snapshot-b"]);
    expect(chain.entries[0].replacedBy?.id).toBe("snapshot-b");
    expect(chain.entries[1].replaces?.id).toBe("snapshot-a");
    expect(chain.current?.id).toBe("snapshot-b");
    expect(chain.pairs).toEqual([{ superseded: snapshots[1], successor: snapshots[0] }]);
    expect(snapshotChain([]).current).toBeNull();
  });

  it("reads the primary metric rows of one overview", () => {
    expect(primaryMetricRows(overview(0.1, "passed", null))).toHaveLength(1);
    expect(primaryMetricRows(null)).toEqual([]);
  });

  it("flags every changed value between a superseded and a current overview", () => {
    const rows = sideBySide(overview(0.1, "passed", null), overview(0.1, "passed", 400_000_000));
    const byKey = new Map(rows.map((row) => [row.key, row]));
    expect(byKey.get("a-vs-b:estimate")).toMatchObject({ left: "+10.0 pp", right: "+10.0 pp", changed: false });
    expect(byKey.get("a-vs-b:gate")).toMatchObject({ changed: false });
    expect(byKey.get("cost")).toMatchObject({ left: "No resource ledger", right: "0.4 USD", changed: true });
    expect(byKey.get("cost_per_success")).toMatchObject({ right: "0.2 USD", changed: true });
    const regressed = sideBySide(overview(0.1, "passed", null), overview(-0.2, "failed", null));
    expect(new Map(regressed.map((row) => [row.key, row])).get("a-vs-b:gate")).toMatchObject({ left: "passed", right: "failed", changed: true });
  });

  it("labels a supersession reason for people", () => {
    expect(supersessionReasonLabel("late_charge_changed_cost_rule")).toBe("Late charge changed cost rule");
    expect(supersessionReasonLabel(null)).toBe("");
  });
});
