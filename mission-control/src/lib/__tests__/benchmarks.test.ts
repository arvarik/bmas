import { describe, expect, it } from "vitest";
import { primaryMetric, runProgress, scoringBadge, statusLabel, type BenchmarkRun } from "@/lib/benchmarks";

describe("benchmark presentation", () => {
  it("calculates bounded progress", () => {
    expect(runProgress({ completed_attempts: 3, total_attempts: 4 })).toBe(75);
    expect(runProgress({ completed_attempts: 1, total_attempts: 0 })).toBe(0);
  });

  it("labels machine states", () => {
    expect(statusLabel("partial_failure")).toBe("Partial failure");
  });

  it("reads the server primary metric and never averages in the browser", () => {
    const run = {
      primary_scorer_id: "scorer-exact",
      primary_scorer_name: "Exact match",
      primary_metric_mean: 0.75,
      primary_metric_count: 4,
    } as BenchmarkRun;
    expect(primaryMetric(run)).toEqual({
      scorer_id: "scorer-exact",
      scorer_name: "Exact match",
      mean: 0.75,
      count: 4,
    });
    expect(primaryMetric({} as BenchmarkRun)).toBeNull();
  });

  it("labels failed scoring so failed work never looks complete", () => {
    expect(scoringBadge({ scoring_status: "failed" } as BenchmarkRun)).toBe("Scoring failed");
    expect(scoringBadge({ analysis_status: "blocked" } as BenchmarkRun)).toBe("Analysis blocked");
    expect(scoringBadge({ scoring_status: "completed", analysis_status: "valid" } as BenchmarkRun)).toBeNull();
  });
});
