/**
 * The metric lifecycle presentation mirrors the daemon transitions,
 * builds a complete publishable definition from the form, and names
 * every missing field before submission.
 */
import { describe, expect, it } from "vitest";

import {
  advanceRequest,
  buildMetricDefinition,
  calibrationSummary,
  defaultMetricForm,
  lifecycleSteps,
  metricFormErrors,
  nextTransitions,
} from "@/lib/metric-lifecycle-presentation";

describe("metric lifecycle presentation", () => {
  it("walks draft, validated, and published with one current step", () => {
    expect(lifecycleSteps("draft").map((step) => step.status)).toEqual(["current", "upcoming", "upcoming"]);
    expect(lifecycleSteps("validated").map((step) => step.status)).toEqual(["done", "current", "upcoming"]);
    expect(lifecycleSteps("published").map((step) => step.status)).toEqual(["done", "done", "current"]);
    expect(lifecycleSteps("withdrawn").map((step) => `${step.state}:${step.status}`)).toEqual([
      "draft:done", "validated:done", "published:done", "withdrawn:terminal",
    ]);
  });

  it("offers only the transitions the daemon accepts", () => {
    expect(nextTransitions("draft")).toEqual(["validated"]);
    expect(nextTransitions("validated")).toEqual(["published", "draft"]);
    expect(nextTransitions("published")).toEqual(["deprecated", "withdrawn"]);
    expect(nextTransitions("deprecated")).toEqual([]);
  });

  it("builds a complete definition with a current one-year calibration", () => {
    const form = { ...defaultMetricForm(new Date("2026-09-03T00:00:00Z")), metric_id: "metric-journey", scorer_id: "scorer-exact-match-v1" };
    expect(metricFormErrors(form)).toEqual([]);
    const record = buildMetricDefinition(form);
    expect(record.schema_id).toBe("metric-definition");
    expect(record.lifecycle_state).toBe("draft");
    expect(record.calibration).toMatchObject({
      state: "current",
      method: "deterministic",
      calibrated_at: "2026-09-03T00:00:00Z",
      expires_at: "2027-09-03T00:00:00Z",
      result: { limits_failed: false, pinned_digests: {} },
    });
    expect(record.labels.evidence_contract).toEqual(["final_output"]);
    expect(record.exclusions).toEqual([]);
    expect(calibrationSummary(record)).toMatchObject({ complete: true, missing: [], state: "current" });
  });

  it("names every problem that blocks registration", () => {
    const form = { ...defaultMetricForm(), metric_id: "bad id!", configuration_digest: "xyz", range_minimum: 1, range_maximum: 0, evidence_contract: " " };
    const errors = metricFormErrors(form);
    expect(errors).toContain("The metric id uses letters, digits, and - _ . : @ / only.");
    expect(errors).toContain("Select the scorer the metric reads.");
    expect(errors).toContain("The configuration digest is 64 hex characters.");
    expect(errors).toContain("The range minimum stays below the maximum.");
    expect(errors).toContain("Name at least one evidence contract field.");
  });

  it("reports what a calibration still needs for publication", () => {
    const summary = calibrationSummary({ calibration: { state: "due", method: "deterministic", version: "1" } });
    expect(summary.complete).toBe(false);
    expect(summary.missing).toEqual(["dataset", "result", "calibrated_at", "expires_at", "drift_policy"]);
  });

  it("shapes the advance request per target", () => {
    expect(advanceRequest("validated", { now: "2026-09-03T00:00:00Z", evidence: { schema: true, fixture: true, evidence: false } })).toEqual({
      target: "validated", now: "2026-09-03T00:00:00Z", validation_evidence: { schema: true, fixture: true, evidence: false },
    });
    expect(advanceRequest("published", { now: "2026-09-03T00:00:00Z" })).toEqual({ target: "published", now: "2026-09-03T00:00:00Z" });
    expect(advanceRequest("withdrawn", { now: "2026-09-03T00:00:00Z", reason: "superseded" })).toEqual({ target: "withdrawn", now: "2026-09-03T00:00:00Z", reason: "superseded" });
  });
});
