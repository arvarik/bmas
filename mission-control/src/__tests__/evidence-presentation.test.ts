/**
 * Dataset version rows, sandbox rows, evidence sections with their
 * redaction classes, and replay bundle summaries.
 */
import { describe, expect, it } from "vitest";

import { statusWords, type DatasetVersionRecord, type EvidenceRecord, type RedactionReport, type ScoreRecord } from "@/lib/evaluation-operations";
import {
  archiveBytes,
  base64FromBytes,
  boundaryLabel,
  bytesText,
  datasetVersionRows,
  evidenceSections,
  importSummary,
  lineageSummary,
  memberRows,
  redactedPaths,
  redactionCounts,
  sandboxRows,
  splitRows,
  terminalTone,
} from "@/lib/evidence-presentation";

const digest = (letter: string) => letter.repeat(64);

const versionRecord: DatasetVersionRecord = {
  version_id: "version-a", parent_version_id: null, canonical_schema_version: "2", source_lineage: ["source-a"],
  trust_inputs: [{ level: "owner_uploaded", policy_version: "1" }], effective_restrictions: [{ name: "deny_secrets", behavior: "hard" }],
  policy_digest: digest("a"), case_manifest_digest: digest("b"), transformation_recipe_digest: digest("c"),
  split_manifest: { test: ["j-1", "j-2", "j-3", "j-4"], train: ["j-5"] }, asset_digests: [digest("d")], content_digest: digest("e"),
  validation_report_digest: digest("f"), contamination_record_digest: digest("0"), attribution_bundle_digest: digest("1"),
};

const score: ScoreRecord = {
  score_id: "score-a", scorer: { scorer_id: "scorer-a", version: "2" }, evidence_references: {}, dimensions: [{ name: "exact", value: 1 }],
  explanation: "match", status: "scored", error: null,
  sandbox: { boundary: "wasi_component", policy_digest: digest("a"), runtime_digest: digest("b"), component_digest: digest("c"), terminal_class: "completed", replay_eligible: true, fuel_used: 12345 },
};

const report: RedactionReport = {
  secret: ["trace[0].api_key", "final_output"], sensitive: ["trace[1].reviewer_email"], prohibited: ["recovery_events[0].password_hash"],
  detectors: { final_output: "bearer_or_basic_header" }, policy_digest: digest("9"),
};

const evidence: EvidenceRecord = {
  attempt_id: "attempt-a", run_manifest_digest: digest("a"), runtime_specification_digest: digest("b"), case_reference: { case_id: "case-1", asset_ids: [] },
  trace_digest: digest("c"), final_output_digest: digest("d"), final_state_digest: undefined, artifacts: [digest("e")], resources: { tokens: 10 },
  completeness: { level: "complete", unavailable_sections: [] }, seed_evidence: {}, redaction_policy_digest: digest("9"), redaction_report: report,
  failure_classification: null, versions: {}, ledger_references: {},
};

describe("evidence presentation", () => {
  it("lists the dataset version digests, splits, and lineage", () => {
    const rows = datasetVersionRows(versionRecord);
    expect(rows.find((row) => row.label === "Content digest")).toEqual({ label: "Content digest", value: digest("e"), kind: "digest" });
    expect(rows.find((row) => row.label === "Parent version")?.value).toBe("None (first version)");
    expect(splitRows(versionRecord)).toEqual([{ split: "test", count: 4, sample: ["j-1", "j-2", "j-3"] }, { split: "train", count: 1, sample: ["j-5"] }]);
    expect(lineageSummary(versionRecord)).toEqual({ sources: ["source-a"], restrictions: ["Deny secrets (hard)"], trust: ["Owner uploaded policy 1"], assetCount: 1 });
  });

  it("names the boundary, the terminal class, and the fuel of one score", () => {
    expect(boundaryLabel("wasi_component")).toBe("WASI component");
    expect(boundaryLabel(undefined)).toBe("Unrecorded");
    const rows = new Map(sandboxRows(score).map((row) => [row.label, row.value]));
    expect(rows.get("Boundary")).toBe("WASI component");
    expect(rows.get("Terminal class")).toBe("Completed");
    expect(rows.get("Fuel used")).toBe("12,345");
    expect(rows.get("Replay eligible")).toBe("Yes");
    expect(rows.get("Component digest")).toBe(digest("c"));
    expect(terminalTone(score)).toBe("passed");
    const trapped: ScoreRecord = { ...score, status: "error", error: "fuel exhausted", sandbox: { ...score.sandbox!, terminal_class: "fuel_exhausted", replay_eligible: false } };
    expect(terminalTone(trapped)).toBe("failed");
    expect(new Map(sandboxRows(trapped).map((row) => [row.label, row.value])).get("Trap or limit")).toBe("fuel exhausted");
    expect(sandboxRows({ ...score, sandbox: undefined })[0].value).toBe("Unrecorded (legacy score)");
  });

  it("lists the persisted sections and the redactions inside each", () => {
    expect(evidenceSections(evidence).map((section) => section.section)).toEqual(["trace", "final_output", "artifact"]);
    expect(redactedPaths(report, "trace")).toEqual([
      { path: "trace[0].api_key", dataClass: "secret", detector: null },
      { path: "trace[1].reviewer_email", dataClass: "sensitive", detector: null },
    ]);
    expect(redactedPaths(report, "final_output")).toEqual([{ path: "final_output", dataClass: "secret", detector: "bearer_or_basic_header" }]);
    expect(redactedPaths(report)).toHaveLength(4);
    expect(redactedPaths(null)).toEqual([]);
    expect(redactionCounts(report)).toEqual({ secret: 2, sensitive: 1, prohibited: 1, total: 4, policyDigest: digest("9") });
  });

  it("groups bundle members and summarizes an import", () => {
    const rows = memberRows({ members: [
      { path: "records/a.json", class: "record", size_bytes: 100, digest: digest("a") },
      { path: "records/b.json", class: "record", size_bytes: 200, digest: digest("b") },
      { path: "artifacts/c", class: "artifact", size_bytes: 3000, digest: digest("c") },
    ] });
    expect(rows).toEqual([{ memberClass: "record", label: "Record", count: 2, bytes: 300 }, { memberClass: "artifact", label: "Artifact", count: 1, bytes: 3000 }]);
    expect(bytesText(3000)).toBe("2.9 KiB");
    const approved = importSummary({ import_id: digest("1"), ingestion_state: "readable", quarantined_members: [], stripped_fields: [], readable_members: ["a", "b"], replay_approved: true, execution_repeat: "not_guaranteed_by_this_bundle", replay: { analysis_replayable: true, claim: "analysis_replayable", results_digest: digest("2"), expected_results_digest: digest("2"), execution_repeat: "not_guaranteed_by_this_bundle" } });
    expect(approved.title).toBe("Replay verified");
    expect(approved.tone).toBe("passed");
    expect(new Map(approved.rows.map((row) => [row.label, row.value])).get("Readable members")).toBe("2");
    const plain = importSummary({ import_id: digest("1"), ingestion_state: "quarantined", quarantined_members: ["x"], stripped_fields: ["y"], readable_members: [], replay_approved: false, execution_repeat: { import_id: digest("1"), requires: ["new_run_plan", "new_capability_decision"], started: false, reason: "agents run again" } });
    expect(plain.title).toBe("Imported without replay approval");
    const plainRows = new Map(plain.rows.map((row) => [row.label, row.value]));
    expect(plainRows.get("Quarantined members")).toBe("x");
    expect(plainRows.get("Execution repeat")).toBe("Not started; requires new run plan, new capability decision");
    expect(statusWords({ nested: true })).toBe("{\"nested\":true}");
  });

  it("round-trips an archive through base64", () => {
    const bytes = new Uint8Array([0, 1, 2, 250, 255]);
    expect(archiveBytes(base64FromBytes(bytes))).toEqual(bytes);
  });
});
