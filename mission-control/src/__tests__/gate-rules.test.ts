/**
 * Frozen gate rules fix their operator per method and serialize only
 * the fields the daemon accepts, and the metric options offer the
 * frozen paths beside the legacy ones.
 */
import { describe, expect, it } from "vitest";

import { frozenMetricOptions, isFrozenMetric, reportMetricOptions, supportedAnalysisMethods, type BenchmarkRunReport, type FrozenRunReport } from "@/lib/benchmarks";
import { ruleShapeForMethod, serializeRules } from "@/lib/gate-rules";

const legacy = {
  arms: [{ arm_slug: "classic-a", arm_name: "Classic A", scorers: [{ scorer_id: "exact", scorer_name: "Exact" }] }],
  comparisons: [],
} as unknown as BenchmarkRunReport;

describe("frozen gate rules", () => {
  it("fixes the operator for each frozen method", () => {
    expect(ruleShapeForMethod("frozen_non_inferiority")).toEqual({ operator: "max_drop", limitLabel: "Margin (0 to 1)", frozen: true });
    expect(ruleShapeForMethod("frozen_superiority")).toEqual({ operator: "gte", limitLabel: "Limit (unused, 0)", frozen: true });
    expect(ruleShapeForMethod("point_estimate").frozen).toBe(false);
  });

  it("serializes only the fields the daemon accepts", () => {
    const rules = serializeRules([
      { id: "frozen", label: "Margin", metric: "frozen.exact", operator: "lte", value: 0.2, analysis_method: "frozen_non_inferiority", direction: "improvement", resample_count: 199, minimum_usable_cases: 1 },
      { id: "better", label: "Better", metric: "frozen.exact.classic-a", operator: "max_drop", value: 0.9, analysis_method: "frozen_superiority", direction: null, resample_count: null, minimum_usable_cases: null },
      { id: "cost", label: "Cost", metric: "arm.classic-a.cost_usd.mean", operator: "lte", value: 100, analysis_method: "point_estimate", direction: null, resample_count: 999 },
    ]);
    expect(rules[0]).toEqual({ id: "frozen", label: "Margin", metric: "frozen.exact", operator: "max_drop", value: 0.2, analysis_method: "frozen_non_inferiority", direction: "improvement", resample_count: 199, minimum_usable_cases: 1 });
    expect(rules[1]).toEqual({ id: "better", label: "Better", metric: "frozen.exact.classic-a", operator: "gte", value: 0, analysis_method: "frozen_superiority", direction: "improvement" });
    expect(rules[2]).toEqual({ id: "cost", label: "Cost", metric: "arm.classic-a.cost_usd.mean", operator: "lte", value: 100, analysis_method: "point_estimate" });
  });

  it("offers frozen metric paths from a legacy report and a frozen report", () => {
    expect(frozenMetricOptions(legacy)).toEqual([
      { value: "frozen.exact", label: "Frozen Exact across runs (first arm)" },
      { value: "frozen.exact.classic-a", label: "Frozen Exact across runs (Classic A)" },
    ]);
    expect(reportMetricOptions(legacy)).toContainEqual({ value: "frozen.exact", label: "Frozen Exact across runs (first arm)" });
    const frozen = { engine: "bmas-frozen-analysis", comparisons: [{ metric: "exact" }], arms: { "classic-a": {}, "classic-b": {} } } as unknown as FrozenRunReport;
    expect(reportMetricOptions(frozen).map((option) => option.value)).toEqual(["frozen.exact", "frozen.exact.classic-a", "frozen.exact.classic-b"]);
  });

  it("supports only the frozen methods on a frozen metric", () => {
    expect(isFrozenMetric("frozen.exact")).toBe(true);
    expect(supportedAnalysisMethods("frozen.exact")).toEqual(["frozen_non_inferiority", "frozen_superiority"]);
    expect(supportedAnalysisMethods("arm.classic-a.score.exact")).not.toContain("frozen_non_inferiority");
  });
});
