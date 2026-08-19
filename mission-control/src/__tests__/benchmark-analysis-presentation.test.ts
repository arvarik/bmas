import { describe, expect, it } from "vitest";
import { formatMetric, reportMetricOptions, type BenchmarkRunReport } from "@/lib/benchmarks";

const report = {
  arms: [{
    arm_slug: "classic",
    arm_name: "Classic",
    scorers: [{ scorer_id: "exact-v1", scorer_name: "Exact" }],
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
  });
});
