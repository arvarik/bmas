import { describe, expect, it } from "vitest";
import { formatMetric, reportMetricOptions, supportedAnalysisMethods, type BenchmarkRunReport } from "@/lib/benchmarks";

const report = {
  arms: [{
    arm_slug: "classic",
    arm_name: "Classic",
    scorers: [{ scorer_id: "exact-v1", scorer_name: "Exact" }],
  }],
  comparisons: [{
    left_arm_slug: "classic",
    left_arm_name: "Classic",
    right_arm_slug: "patchboard",
    right_arm_name: "Patchboard",
    scorers: [{ scorer_id: "exact-v1" }],
  }],
} as BenchmarkRunReport;

describe("benchmark analysis presentation", () => {
  it("formats unavailable metrics without inventing zero values", () => {
    expect(formatMetric(null, "percent")).toBe("Unavailable");
    expect(formatMetric(0, "cost")).toBe("$0.0000");
  });

  it("builds stable regression metric paths from arm slugs", () => {
    expect(reportMetricOptions(report)).toContainEqual({
      value: "arm.classic.score.exact-v1",
      label: "Classic Exact score",
    });
    expect(reportMetricOptions(report)).toContainEqual({
      value: "arm.classic.duration_ms.p95",
      label: "Classic p95 duration",
    });
    expect(reportMetricOptions(report)).toContainEqual({
      value: "comparison.classic.patchboard.score.exact-v1",
      label: "Classic to Patchboard exact-v1 paired difference",
    });
  });

  it("offers only analysis methods that the selected metric supports", () => {
    expect(supportedAnalysisMethods("arm.classic.failure_rate")).toEqual([
      "point_estimate",
    ]);
    expect(supportedAnalysisMethods("arm.classic.cost_usd.mean")).toContain(
      "lower_confidence_bound",
    );
    expect(supportedAnalysisMethods("comparison.classic.patchboard.score.exact-v1")).toContain(
      "holm_sign_test",
    );
  });
});
