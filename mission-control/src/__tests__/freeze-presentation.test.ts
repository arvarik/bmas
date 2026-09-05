/**
 * The freeze form derives families and arms from the run's attempts,
 * validates its bounds, and builds the daemon's freeze request.
 */
import { describe, expect, it } from "vitest";

import type { BenchmarkAttempt } from "@/lib/benchmarks";
import { armOptions, buildFreezeRequest, defaultFreezeForm, familiesFromAttempts, freezeFormErrors } from "@/lib/freeze-presentation";

function attempt(overrides: Partial<BenchmarkAttempt>): BenchmarkAttempt {
  return {
    id: "attempt", trial_id: "trial", arm_name: "Classic A", item_key: "j-1", repeat_index: 0, retry_index: 0, status: "completed", task_id: null,
    failure_category: null, error_message: null, total_cost_usd: null, total_tokens: null, duration_ms: null, result_summary: null,
    arm_id: "classic-a", dataset_item_id: "item-1", subject: "arithmetic", ...overrides,
  };
}

const attempts = [
  attempt({ id: "a1" }),
  attempt({ id: "a2", dataset_item_id: "item-2", subject: "words" }),
  attempt({ id: "a3", arm_id: "classic-b", arm_name: "Classic B", repeat_index: 1 }),
  attempt({ id: "a4", arm_id: "classic-b", arm_name: "Classic B", dataset_item_id: "item-3", subject: null }),
];

describe("freeze presentation", () => {
  it("derives families from subjects and arms from the attempts", () => {
    expect(familiesFromAttempts(attempts)).toEqual({ all: ["item-3"], arithmetic: ["item-1"], words: ["item-2"] });
    expect(armOptions(attempts)).toEqual([{ arm_id: "classic-a", arm_name: "Classic A" }, { arm_id: "classic-b", arm_name: "Classic B" }]);
    expect(familiesFromAttempts([])).toEqual({});
  });

  it("defaults the form from the run and validates its bounds", () => {
    const form = defaultFreezeForm(attempts, "scorer-a");
    expect(form).toMatchObject({ scorer_id: "scorer-a", planned_repetitions: 2, baseline_arm: "classic-a", candidate_arm: "classic-b", resample_count: 999 });
    expect(freezeFormErrors(form, attempts)).toEqual([]);
    expect(freezeFormErrors({ ...form, resample_count: 0 }, attempts)).toContain("Resamples is 1 to 100000.");
    expect(freezeFormErrors({ ...form, scorer_id: "" }, attempts)).toContain("Select the scorer the estimand reads.");
    expect(freezeFormErrors(form, [])).toContain("The run has no attempts to build families from.");
  });

  it("builds the freeze request with one predeclared comparison", () => {
    const form = { ...defaultFreezeForm(attempts, "scorer-a"), metric_ids: ["metric-a"], non_inferiority_margin: 0.2 };
    expect(buildFreezeRequest(form, attempts)).toEqual({
      families: { all: ["item-3"], arithmetic: ["item-1"], words: ["item-2"] },
      scorer_id: "scorer-a", master_seed: 7, planned_repetitions: 2, resample_count: 999, confidence_level: 0.95, binary_reduction: "strict_majority", metric_ids: ["metric-a"],
      comparison_family: { family_id: "browser", comparisons: [{ comparison_id: "a-vs-b", baseline_arm: "classic-a", candidate_arm: "classic-b", non_inferiority_margin: 0.2 }] },
    });
  });
});
