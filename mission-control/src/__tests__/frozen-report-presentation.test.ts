/**
 * The frozen report presentation formats daemon numbers without
 * recomputing them and lays the decision bar out so the interval,
 * the estimate, zero, and the margin share one axis.
 */
import { describe, expect, it } from "vitest";

import type { FrozenComparison, FrozenRunReport } from "@/lib/benchmarks";
import {
  decisionSummary,
  forestGeometry,
  formatDifference,
  formatIntervalText,
  metricResolutionSummary,
  replayVerificationLabel,
  reportEngineLabel,
  ruleComparison,
} from "@/lib/frozen-report-presentation";

const comparison: FrozenComparison = {
  comparison_id: "a-vs-b",
  metric: "exact",
  baseline_arm: "classic-a",
  candidate_arm: "classic-b",
  direction: "higher_is_better",
  hypothesis: "non_inferiority",
  non_inferiority_margin: 0.2,
  minimum_usable_cases: 1,
  estimate: 0.05,
  interval: { status: "estimated", low: -0.1, high: 0.2, method: "family_stratified_weighted_case_bootstrap_percentile", unit: "case", replicate_count: 99 },
  test: { method: "paired_sign_flip", mode: "monte_carlo", p_value: 0.42, resamples: 99 },
  p_value_adjusted: 0.42,
  counts: { paired_cases: 3, missing_cases: 0, removed_slots: 0 },
  limit_failures: [],
  primary_valid: true,
  small_families: [],
  comparative_claim: true,
  statistical_unit: "case",
  gate: { status: "passed", reasons: [], bound: -0.1, margin: 0.2, rule: "lower_bound_above_negative_margin" },
};

function frozenReport(overrides: Partial<FrozenRunReport> = {}): FrozenRunReport {
  return {
    engine: "bmas-frozen-analysis",
    engine_version: "1",
    snapshot_id: "snapshot-1",
    replay_verified: true,
    results_digest: "a".repeat(64),
    stored_results_digest: "a".repeat(64),
    metrics: [],
    unresolved_metrics: [],
    analysis: { estimand: "family-balanced-unconditional-task-success", statistical_unit: "case", specification_digest: "b".repeat(64), replay_claim: "analysis_replayable" },
    denominators: { planned: 6, statement: "planned slots minus predeclared infrastructure exclusions" },
    comparisons: [comparison],
    arms: {},
    resources: { available: false, statement: "no resource ledger" },
    warnings: [],
    report: { metric_ids: [], results_digest: "a".repeat(64), input_digest: "c".repeat(64) },
    ...overrides,
  };
}

describe("frozen report presentation", () => {
  it("formats differences in percentage points without inventing values", () => {
    expect(formatDifference(0.05)).toBe("+5.0 pp");
    expect(formatDifference(-0.125)).toBe("-12.5 pp");
    expect(formatDifference(null)).toBe("Unavailable");
    expect(formatIntervalText(comparison.interval)).toBe("-10.0 pp to +20.0 pp");
    expect(formatIntervalText({ status: "insufficient", low: null, high: null })).toBe("Insufficient cases");
  });

  it("explains a passed non-inferiority decision with its bound and margin", () => {
    const summary = decisionSummary(comparison.gate, "non_inferiority");
    expect(summary.tone).toBe("passed");
    expect(summary.rule).toContain("lower bound stays above the negative margin");
    expect(summary.detail).toBe("Bound -10.0 pp, margin -20.0 pp.");
  });

  it("names every blocking reason of an indeterminate decision", () => {
    const summary = decisionSummary({ status: "indeterminate", reasons: ["insufficient_family_cluster", "no_comparative_interval"] }, "non_inferiority");
    expect(summary.label).toBe("Indeterminate");
    expect(summary.detail).toBe("Blocked by insufficient family cluster, no comparative interval.");
  });

  it("lays the interval, estimate, zero, and margin on one axis", () => {
    const geometry = forestGeometry(comparison, 200);
    expect(geometry.zeroX).toBe(100);
    expect(geometry.marginX).not.toBeNull();
    expect(geometry.marginX as number).toBeLessThan(geometry.zeroX);
    expect(geometry.lowX as number).toBeLessThan(geometry.estimateX as number);
    expect(geometry.estimateX as number).toBeLessThan(geometry.highX as number);
    expect(geometry.ticks.map((tick) => tick.label)).toEqual(["-23", "0", "+23"]);
    const empty = forestGeometry({ ...comparison, interval: { status: "insufficient", low: null, high: null } });
    expect(empty.lowX).toBeNull();
    expect(empty.highX).toBeNull();
  });

  it("puts the margin on the high side when lower is better", () => {
    const geometry = forestGeometry({ ...comparison, direction: "lower_is_better" }, 200);
    expect(geometry.marginX as number).toBeGreaterThan(geometry.zeroX);
  });

  it("labels the engine, the replay verification, and the metric resolution", () => {
    const report = frozenReport();
    expect(reportEngineLabel(report)).toEqual({ label: "Frozen snapshot", tone: "frozen" });
    expect(replayVerificationLabel(report)).toEqual({ label: "Replay verified", tone: "passed" });
    expect(metricResolutionSummary(report).status).toBe("none");
    const resolved = frozenReport({ metrics: [{ metric_id: "metric-a", calibration_version: "1", lifecycle_state: "published", scorer: { scorer_id: "exact", version: "1", configuration_digest: "d".repeat(64) }, measurement: { numerator: "n", denominator: "d", unit: "proportion", range: { minimum: 0, maximum: 1 }, direction: "higher_is_better", aggregation: "mean" } }] });
    expect(metricResolutionSummary(resolved)).toMatchObject({ status: "resolved", resolved: 1, unresolved: 0 });
    const partial = frozenReport({ metrics: resolved.metrics, unresolved_metrics: [{ metric_id: "metric-b", reason: "no registered definition" }] });
    expect(metricResolutionSummary(partial).status).toBe("partial");
    expect(replayVerificationLabel(frozenReport({ replay_verified: false })).tone).toBe("failed");
  });

  it("turns a frozen gate rule into a comparison for the decision bar", () => {
    const rule = {
      value: 0.2,
      analysis_method: "frozen_non_inferiority",
      direction: "improvement" as const,
      frozen: { engine: "bmas-frozen-analysis", estimate: 0.05, interval: comparison.interval },
    };
    expect(ruleComparison(rule)).toEqual({
      estimate: 0.05,
      interval: comparison.interval,
      non_inferiority_margin: 0.2,
      direction: "higher_is_better",
      hypothesis: "non_inferiority",
    });
    expect(ruleComparison({ value: 0, frozen: { engine: "bmas-frozen-analysis" } })).toBeNull();
  });
});
