/**
 * The anchor set form validates and builds the registration, the
 * schedule status reads the next due time, and the agreement summary
 * formats raw agreement, kappa, drift, and abstention.
 */
import { describe, expect, it } from "vitest";

import {
  agreementSummary,
  anchorSetFormErrors,
  buildAnchorSetRequest,
  defaultAnchorSetForm,
  parseLabelItems,
  scheduleStatus,
} from "@/lib/judge-calibration-presentation";
import type { CalibrationRecord } from "@/lib/evaluation-operations";

function completeForm() {
  return { ...defaultAnchorSetForm(), anchor_id: "anchor-a", judge_id: "judge-a", judge_model: "model-x", scorer_id: "scorer-a", dataset_id: "dataset-a", items: "item-1, pass\nitem-2\tfail\n\nitem-3, pass" };
}

function calibration(overrides: Partial<CalibrationRecord> = {}): CalibrationRecord {
  return {
    calibration_id: "calibration-a",
    judge: { judge_id: "judge-a", version: "2", model: "model-x", prompt_digest: "a".repeat(64) },
    scorer: { scorer_id: "scorer-a", version: "1" },
    dataset: { dataset_id: "dataset-a", version: "1", label_digest: "b".repeat(64), item_count: 12 },
    agreement: { raw: 0.8333, kappa: 0.66, kappa_defined: true, interval: { low: 0.55, high: 0.95 } },
    disagreement: { count: 2, item_ids: ["item-2", "item-5"] },
    invalid_output: { count: 0, rate: 0 },
    abstention: { count: 1, rate: 0.0833 },
    drift: { previous_version: "1", raw_agreement_delta: -0.05, exceeds_policy: false },
    state: "current",
    threshold: 0.7,
    calibrated_at: "2026-09-04T00:00:00Z",
    ...overrides,
  };
}

describe("judge calibration presentation", () => {
  it("parses labelled items with comma or tab separators", () => {
    expect(parseLabelItems("item-1, pass\nitem-2\tfail\nbad\n")).toEqual([{ item_id: "item-1", label: "pass" }, { item_id: "item-2", label: "fail" }]);
    expect(parseLabelItems("item-3, fail, 16\nitem-4, pass, the answer is 42, really")).toEqual([
      { item_id: "item-3", label: "fail", candidate: "16" },
      { item_id: "item-4", label: "pass", candidate: "the answer is 42, really" },
    ]);
  });

  it("names every problem and builds the registration", () => {
    const errors = anchorSetFormErrors(defaultAnchorSetForm());
    expect(errors).toContain("The anchor id uses letters, digits, and - _ . : @ / only.");
    expect(errors).toContain("List at least two labelled items, one per line as id, label.");
    expect(anchorSetFormErrors(completeForm())).toEqual([]);
    expect(anchorSetFormErrors({ ...completeForm(), items: "item-1, a\nitem-1, b" })).toContain("Every anchor item id is unique.");
    const request = buildAnchorSetRequest({ ...completeForm(), candidate_models: "model-y, model-z" }, "2026-09-04T00:00:00Z");
    expect(request).toMatchObject({ anchor_id: "anchor-a", judge_id: "judge-a", judge_version: "1", interval_days: 7, threshold: 0.7, drift_tolerance: 0.1, registered_at: "2026-09-04T00:00:00Z", candidate_models: ["model-y", "model-z"] });
    expect(request.label_set.items).toHaveLength(3);
  });

  it("reads the schedule status against one moment", () => {
    const now = new Date("2026-09-04T00:00:00Z");
    expect(scheduleStatus({ state: "active", next_due_at: "2026-09-11T00:00:00Z", due: false }, now)).toMatchObject({ label: "Due in 7 days", tone: "passed" });
    expect(scheduleStatus({ state: "active", next_due_at: "2026-09-04T00:00:00Z", due: true }, now)).toMatchObject({ label: "Due now", tone: "failed" });
    expect(scheduleStatus({ state: "active", next_due_at: "2026-09-01T00:00:00Z", due: true }, now)).toMatchObject({ label: "Overdue by 3 days", tone: "failed" });
    expect(scheduleStatus({ state: "retired", next_due_at: "2026-09-01T00:00:00Z", due: false }, now)).toMatchObject({ label: "Retired", tone: "cancelled" });
  });

  it("formats the agreement summary with kappa, interval, drift, and abstention", () => {
    const summary = agreementSummary(calibration());
    expect(summary).toMatchObject({ available: true, tone: "passed", raw: "83.3%", kappa: "0.66", interval: "55.0% to 95.0%", threshold: "70%", driftDelta: "-5.0 pp against 1", exceedsPolicy: false, abstention: "8.3% (1)", invalidOutput: "0.0% (0)", disagreements: "2 of 12" });
    const failed = agreementSummary(calibration({ state: "failed", agreement: { raw: 0.5, kappa: null, kappa_defined: false, interval: null }, drift: { previous_version: null, raw_agreement_delta: null, exceeds_policy: true } }));
    expect(failed).toMatchObject({ tone: "failed", kappa: "Undefined (one label class)", interval: "", driftDelta: "No previous version", exceedsPolicy: true });
    expect(agreementSummary(null)).toMatchObject({ available: false, raw: "Not calibrated" });
  });
});
