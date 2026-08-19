import { describe, expect, it } from "vitest";
import { runProgress, scoreSummary, statusLabel, type BenchmarkRun } from "@/lib/benchmarks";

describe("benchmark presentation", () => {
  it("calculates bounded progress", () => {
    expect(runProgress({ completed_attempts: 3, total_attempts: 4 })).toBe(75);
    expect(runProgress({ completed_attempts: 1, total_attempts: 0 })).toBe(0);
  });

  it("labels machine states", () => {
    expect(statusLabel("partial_failure")).toBe("Partial failure");
  });

  it("uses only the latest retry for a score summary", () => {
    const run = {
      attempts: [
        { id: "a1", trial_id: "t1", repeat_index: 1, retry_index: 0 },
        { id: "a2", trial_id: "t1", repeat_index: 1, retry_index: 1 },
      ],
      scores: [
        { attempt_id: "a1", status: "scored", score: 0 },
        { attempt_id: "a2", status: "scored", score: 1 },
      ],
    } as BenchmarkRun;
    expect(scoreSummary(run)).toBe(1);
  });
});
