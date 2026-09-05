/**
 * The study form builds the daemon's study input, names every problem
 * before submission, shows the estimate rows, and groups the admission
 * verdict into passed and failed checks.
 */
import { describe, expect, it } from "vitest";

import {
  buildStudyRequest,
  defaultStudyForm,
  durationText,
  estimateRows,
  parseFamilies,
  studyFormErrors,
  verdictSummary,
} from "@/lib/study-presentation";

function readyForm() {
  return {
    ...defaultStudyForm(),
    name: "temperature",
    dataset_version_id: "version-a",
    case_ids: "item-0, item-1\nitem-2",
    scorer_id: "scorer-exact-match",
  };
}

describe("study presentation", () => {
  it("names every problem of an empty form and none of a complete one", () => {
    const errors = studyFormErrors(defaultStudyForm());
    expect(errors).toContain("The study needs a name.");
    expect(errors).toContain("Select the dataset version the study runs on.");
    expect(errors).toContain("List at least one case id.");
    expect(errors).toContain("Select the scorer the estimand reads.");
    expect(studyFormErrors(readyForm())).toEqual([]);
    expect(studyFormErrors({ ...readyForm(), base_configuration: "[1]" })).toContain("The base configuration is one JSON object.");
    expect(studyFormErrors({ ...readyForm(), treatment_values: "only-one" })).toContain("The treatment needs at least two values, one per arm.");
    expect(studyFormErrors({ ...readyForm(), families: "math: item-0" })).toContain("Every case belongs to one family; 2 cases have none.");
    expect(studyFormErrors({ ...readyForm(), per_attempt_cost: "abc" })).toContain("The cost per attempt is a decimal amount such as 0.005.");
  });

  it("parses families and defaults every case into one family", () => {
    expect(parseFamilies("math: item-0, item-1\nwords: item-2", ["item-0", "item-1", "item-2"])).toEqual({ math: ["item-0", "item-1"], words: ["item-2"] });
    expect(parseFamilies("", ["item-0", "item-1"])).toEqual({ all: ["item-0", "item-1"] });
    expect(parseFamilies("bad line", [])).toEqual({});
  });

  it("builds the study input for a preview and for publication", () => {
    const preview = buildStudyRequest(readyForm(), { publish: false });
    expect(preview.publish).toBe(false);
    expect(preview.scorer_versions).toEqual([]);
    expect(preview.treatment).toEqual({ path: "classic.max_rounds", values: [4, 6] });
    expect(buildStudyRequest({ ...readyForm(), treatment_values: "starter-model, other-model" }, { publish: false }).treatment.values).toEqual(["starter-model", "other-model"]);
    expect(preview.invariants).toMatchObject({ dataset_version_id: "version-a", case_ids: ["item-0", "item-1", "item-2"], seed_schedule: { base_seed: 11 }, scorers: ["scorer-exact-match"], arm_order: "rotated_interleave", repetitions: 1 });
    expect(preview.families).toEqual({ all: ["item-0", "item-1", "item-2"] });
    expect(preview.per_attempt_cost).toEqual({ currency: "USD", amount_nanos: 5000000 });
    expect(preview.base_configuration).toEqual({ classic: { max_rounds: 4 } });
    const published = buildStudyRequest(readyForm(), { publish: true, authoredAt: "2026-09-04T00:00:00Z" });
    expect(published.publish).toBe(true);
    expect(published.scorer_versions).toEqual([{ id: "scorer-exact-match", configuration: {} }]);
    expect(published.authored_at).toBe("2026-09-04T00:00:00Z");
  });

  it("formats the estimate rows and durations", () => {
    const rows = estimateRows({
      arms: [{ slug: "model-a", treatment: "model-a", configuration_digest: "a", configuration: {} }, { slug: "model-b", treatment: "model-b", configuration_digest: "b", configuration: {} }],
      sample_plan: { cases: 4, repetitions: 2, arms: 2, attempts: 16, families: 1 },
      estimates: { attempts: 16, cost: { currency: "USD", amount_nanos: 80000000 }, pricing_basis: "per_attempt_reservation", duration_seconds: 60, max_concurrency: 4 },
    });
    const byLabel = new Map(rows.map((row) => [row.label, row.value]));
    expect(byLabel.get("Arms")).toBe("2 (model-a, model-b)");
    expect(byLabel.get("Attempts")).toBe("16");
    expect(byLabel.get("Estimated cost")).toBe("0.08 USD (Per attempt reservation)");
    expect(byLabel.get("Estimated duration")).toBe("1.0 min at concurrency 4");
    expect(durationText(30)).toBe("30 s");
    expect(durationText(7200)).toBe("2.0 h");
    expect(durationText(-1)).toBe("Unavailable");
  });

  it("summarizes an admission verdict", () => {
    const blocked = verdictSummary({ ready: false, blocking: ["source_pinned"], checks: [{ check: "source_pinned", passed: false, detail: "no source" }, { check: "holdout_hidden", passed: true, detail: "" }] });
    expect(blocked.title).toBe("Admission blocked by 1 condition");
    expect(blocked.tone).toBe("failed");
    expect(blocked.failed.map((check) => check.check)).toEqual(["source_pinned"]);
    expect(blocked.passed).toHaveLength(1);
    expect(verdictSummary({ ready: true, blocking: [], checks: [] })).toMatchObject({ title: "Admission ready", tone: "passed" });
    expect(verdictSummary(null)).toMatchObject({ title: "No study conditions", tone: "indeterminate" });
  });
});
