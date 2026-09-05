import { expect, test, type Page } from "@playwright/test";

/**
 * The evaluation operations screens against mocked daemon responses:
 * the study verdict, the resource ledger with a late charge, the
 * per-attempt score boundary and redaction viewer, the browser freeze,
 * the replay bundle export and import, the study authoring page, the
 * judge calibration page, and the dataset version record.
 */
const now = "2026-09-04T12:00:00Z";
const digest = (letter: string) => letter.repeat(64);
const usd = (nanos: number) => ({ currency: "USD", amount_nanos: nanos });

async function mockShell(page: Page) {
  await page.route("**/api/tasks?**", async (route) => {
    await route.fulfill({ json: { tasks: [], total: 0, grand_total: 0, limit: 50, offset: 0 } });
  });
  await page.route("**/api/stream/system", async (route) => {
    await route.fulfill({
      contentType: "text/event-stream",
      body: `event: daemon_status\ndata: ${JSON.stringify({ status: "ready", timestamp: now })}\n\n`,
    });
  });
  await page.route("**/api/benchmarks/scorers", async (route) => route.fulfill({ json: { scorers: [{ id: "scorer-exact", name: "Exact match", version: "1" }] } }));
}

const attempt = (id: string, arm: string, item: string, status: string, extra: Record<string, unknown> = {}) => ({
  id, trial_id: `trial-${id}`, arm_name: arm === "classic-a" ? "Classic A" : "Classic B", arm_id: arm, item_key: item, dataset_item_id: item, subject: "arithmetic",
  repeat_index: 0, retry_index: 0, status, task_id: status === "completed" ? `task-${id}` : null, failure_category: null, error_message: null,
  total_cost_usd: 0.01, total_tokens: 100, duration_ms: 1200, result_summary: null, ...extra,
});

const run = {
  id: "run-ops", test_id: "test-ops", test_name: "Operations journey", test_revision_id: "revision-ops", revision: 1, status: "completed",
  total_trials: 2, completed_trials: 2, total_attempts: 2, completed_attempts: 1, total_cost_usd: 0.02, primary_scorer_id: "scorer-exact", primary_scorer_name: "Exact match",
  created_at: now, started_at: now, completed_at: now,
  attempts: [
    attempt("attempt-1", "classic-a", "item-1", "completed"),
    attempt("attempt-2", "classic-b", "item-2", "failed", { failure_category: "configuration", error_message: "The study conditions block admission: source_pinned" }),
  ],
  scores: [], human_reviews: [],
};

const studyRecord = {
  study_type: "one_factor_ablation", name: "temperature", arms: [
    { slug: "model-a", treatment: "model-a", configuration_digest: digest("a"), configuration: {} },
    { slug: "model-b", treatment: "model-b", configuration_digest: digest("b"), configuration: {} },
  ],
  expansion_rule: { study_type: "one_factor_ablation", treatment: {} }, invariants: { dataset_version_id: "version-ops", case_ids: ["item-1", "item-2"], seed_schedule: { base_seed: 11 }, scorers: ["scorer-exact"], arm_order: "rotated_interleave", repetitions: 1 },
  estimand: { hypothesis: "non_inferiority" }, gates: { comparison_family: { comparisons: [{ baseline_arm: "model-a", candidate_arm: "model-b", non_inferiority_margin: 0.05 }] }, predeclared: true },
  sample_plan: { cases: 2, repetitions: 1, arms: 2, attempts: 4, families: 1 },
  estimates: { attempts: 4, cost: usd(20000000), pricing_basis: "per_attempt_reservation", duration_seconds: 60, max_concurrency: 4 },
  treatment_paths: ["classic.max_rounds"], study_digest: digest("c"),
};

const ledgerEntry = (id: string, extra: Record<string, unknown>) => ({
  entry_id: id, resource_class: "runtime", provider: "provider-a", service: "chat", region: "us-east", quantity: { value: 1000, unit: "tokens" }, pricing_version: "pricing-1",
  charge_state: "estimated", estimate: { value: usd(100000000), method: "list_price", estimated_at: now }, references: { run_id: "run-ops", attempt_id: "attempt-1" },
  reservation_id: null, reconciliation_id: null, estimate_entry_id: null, recorded_at: now, ...extra,
});

const ledgerSummary = {
  currency: "USD", estimate_total: usd(100000000), actual_total: usd(0), estimate_error_total: usd(0), entries_with_both: 0,
  unknown_entry_ids: ["entry-unknown"], not_billable_entry_ids: ["entry-storage"],
  per_class: { runtime: { estimate: usd(100000000), actual: usd(0), entries: 2 }, storage: { estimate: usd(0), actual: usd(0), entries: 1 } }, no_use_classes: ["judge"],
};

const reconciliation = (version: number, reason: string, supersedes: string | null) => ({
  id: `settlement-${version}`, run_id: "run-ops", settlement_version: version, record_checksum: digest("d"), created_at: now,
  record: { reconciliation_version: version, reason, reconciled_at: now, currency: "USD", summary: ledgerSummary, reservations: [], unmatched_reservation_references: [], cost_rules: [{ metric: "actual_total", operator: "lte", limit: usd(150000000), observed: usd(0), status: "failed_unknown" }], cost_per_success: null, unconditional_successes: null, supersedes_reconciliation: supersedes, entry_ids: ["entry-estimate"] },
});

const scoreRecord = {
  id: "score-1", attempt_id: "attempt-1", scorer_version_id: "scorer-exact:1", status: "scored", record_checksum: digest("e"), created_at: now,
  record: { score_id: "score-1", scorer: { scorer_id: "scorer-exact", version: "1" }, evidence_references: {}, dimensions: [{ name: "accuracy", value: 1 }], explanation: "exact match", status: "scored", error: null,
    sandbox: { boundary: "wasi_component", policy_digest: digest("1"), runtime_digest: digest("2"), component_digest: digest("3"), terminal_class: "completed", replay_eligible: true, fuel_used: 4321 } },
};

const evidenceRecord = {
  source: "current",
  record: { attempt_id: "attempt-1", run_manifest_digest: digest("4"), runtime_specification_digest: digest("5"), case_reference: { case_id: "item-1", asset_ids: [] }, trace_digest: digest("6"), final_output_digest: digest("7"), artifacts: [],
    resources: { tokens: 100 }, completeness: { level: "complete", unavailable_sections: [] }, seed_evidence: {}, redaction_policy_digest: digest("8"),
    redaction_report: { secret: ["trace[0].api_key"], sensitive: ["trace[0].reviewer_email"], prohibited: [], detectors: {}, policy_digest: digest("8") }, failure_classification: null, versions: {}, ledger_references: {} },
};

const bundleExport = {
  manifest: { format: "bmas-analysis-replay-bundle", format_version: 1, policy: "redacted", redaction_policy_digest: digest("8"), claims: { analysis_replay: "deterministic" }, members: [
    { path: "records/run.json", class: "record", size_bytes: 120, digest: digest("a") },
    { path: "records/snapshot.json", class: "record", size_bytes: 300, digest: digest("b") },
    { path: "artifacts/trace", class: "artifact", size_bytes: 2048, digest: digest("c") },
  ] },
  manifest_digest: digest("9"), bundle_digest: digest("0"), member_count: 3, archive_base64: Buffer.from("PK archive").toString("base64"),
};

async function mockRunPage(page: Page) {
  await page.route("**/api/benchmarks/runs/run-ops", async (route) => route.fulfill({ json: run }));
  await page.route("**/api/benchmarks/runs/run-ops/report**", async (route) => route.fulfill({ status: 503, json: { error: "The report engine is offline" } }));
  await page.route("**/api/evaluation/runs/run-ops/study", async (route) => route.fulfill({ json: { run_id: "run-ops", study_id: "study-1", plan_id: "plan-1", study: studyRecord,
    verdict: { ready: false, blocking: ["source_pinned"], checks: [{ check: "source_pinned", passed: false, detail: "The dataset lineage names no pinned source." }, { check: "holdout_hidden", passed: true, detail: "The holdout stays hidden." }] } } }));
  await page.route("**/api/evaluation/runs/run-ops/resource-ledger**", async (route) => route.fulfill({ json: { run_id: "run-ops", entries: [
    ledgerEntry("entry-estimate", {}),
    ledgerEntry("entry-unknown", { charge_state: "unknown", estimate: undefined }),
    ledgerEntry("entry-storage", { resource_class: "storage", charge_state: "not_billable", estimate: undefined, not_billable_evidence: "local evidence store" }),
  ], summary: ledgerSummary } }));
  await page.route("**/api/evaluation/attempts/attempt-1/score-records", async (route) => route.fulfill({ json: { attempt_id: "attempt-1", scores: [scoreRecord] } }));
  await page.route("**/api/evaluation/attempts/attempt-1/evidence", async (route) => route.fulfill({ json: evidenceRecord }));
  await page.route(`**/api/evaluation/evidence/sections/${digest("6")}`, async (route) => route.fulfill({ json: { redacted: false, value: [{ kind: "tool_call", api_key: "[redacted]", reviewer_email: "[redacted]", argument: "safe" }] } }));
  await page.route("**/api/evaluation/metrics?lifecycle_state=published", async (route) => route.fulfill({ json: { metrics: [{ metric_id: "metric-ops", lifecycle_state: "published", calibration_state: "current", record_checksum: digest("f"), created_at: now, record: {} }] } }));
}

test.use({ viewport: { width: 1280, height: 2200 } });

test.beforeEach(async ({ page }) => {
  await mockShell(page);
});

test("shows the study verdict, the ledger totals, and applies a late charge on the run page", async ({ page }) => {
  await mockRunPage(page);
  const reconciliations = [reconciliation(1, "scheduled", null)];
  await page.route("**/api/evaluation/runs/run-ops/analyses", async (route) => route.fulfill({ json: { run_id: "run-ops", snapshots: [] } }));
  await page.route("**/api/evaluation/runs/run-ops/reconciliations", async (route, request) => {
    if (request.method() === "POST") {
      const body = request.postDataJSON() as { late_charge?: { charge_state: string; estimate_entry_id: string | null; actual: { value: { amount_nanos: number } } } };
      expect(body.late_charge?.charge_state).toBe("confirmed");
      expect(body.late_charge?.estimate_entry_id).toBe("entry-estimate");
      expect(body.late_charge?.actual.value.amount_nanos).toBe(400000000);
      reconciliations.push(reconciliation(2, "late_charge", "settlement-1"));
      return route.fulfill({ status: 201, json: { entry_id: "late-charge-1", reconciliation_id: "settlement-2", reconciliation_version: 2, cost_rule_changed: true, superseded_gates: 1, analysis_recompute_required: true, affected_analysis_snapshot_ids: ["snapshot-1"], recomputed_analysis_snapshots: ["snapshot-2"], recompute_failures: [] } });
    }
    return route.fulfill({ json: { run_id: "run-ops", reconciliations } });
  });

  await page.goto("/runs/run-ops");
  const study = page.getByRole("region", { name: "Study conditions" });
  await expect(study.getByText("Admission blocked by 1 condition")).toBeVisible();
  await expect(study.getByRole("list", { name: "Blocking conditions" })).toContainText("Source pinned");
  await expect(study.getByText("1 attempt failed admission with a configuration failure")).toBeVisible();
  await expect(study.getByRole("cell", { name: "Blocks" })).toBeVisible();
  await expect(study.getByRole("link", { name: "temperature" })).toHaveAttribute("href", "/studies/study-1");
  await expect(page.getByText("Configuration failure", { exact: true })).toBeVisible();

  const ledger = page.getByRole("region", { name: "Resource ledger" });
  await expect(ledger.getByText("3 entries · 1 reconciliation version")).toBeVisible();
  await expect(ledger.getByRole("cell", { name: "Runtime" })).toBeVisible();
  await expect(ledger.getByText("1 entry with an unknown price")).toBeVisible();
  await expect(ledger.getByText("1 not-billable entry")).toBeVisible();
  await expect(ledger.getByText("No use recorded for Judge")).toBeVisible();
  await expect(ledger.getByRole("cell", { name: "First version" })).toBeVisible();
  await expect(ledger.getByText("Failed unknown")).toBeVisible();

  await ledger.getByRole("button", { name: "Apply late charge" }).click();
  const estimateBox = page.getByRole("combobox", { name: "Estimate entry", exact: true });
  await estimateBox.focus();
  await page.keyboard.press("ArrowDown");
  await expect(estimateBox).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("option", { name: /entry-estimate/ }).dispatchEvent("click");
  await expect(page.getByLabel("Provider", { exact: true })).toHaveValue("provider-a");
  await page.getByLabel("Charged amount (USD)").fill("0.40");
  await page.getByLabel("Provider amount text").fill("$0.40");
  await page.getByRole("button", { name: "Store late charge" }).click();
  const outcome = page.getByRole("status", { name: "Late charge outcome" });
  await expect(outcome).toContainText("opened reconciliation version 2");
  await expect(outcome).toContainText("1 gate(s) superseded and 1 of 1 snapshot(s) recomputed");
  await expect(ledger.getByText("Late charge", { exact: true })).toBeVisible();
  await expect(ledger.getByRole("row", { name: /Late charge/ }).getByRole("cell", { name: "settlement-1", exact: true })).toBeVisible();
});

test("shows each score's boundary and the redaction classes in the evidence viewer", async ({ page }) => {
  await mockRunPage(page);
  await page.route("**/api/evaluation/runs/run-ops/analyses", async (route) => route.fulfill({ json: { run_id: "run-ops", snapshots: [] } }));
  await page.route("**/api/evaluation/runs/run-ops/reconciliations", async (route) => route.fulfill({ json: { run_id: "run-ops", reconciliations: [] } }));

  await page.goto("/runs/run-ops");
  await page.getByText("Scores and evidence").first().click();
  const scores = page.getByRole("region", { name: "Score records" }).first();
  await expect(scores.getByText("WASI component")).toBeVisible();
  await expect(scores.getByText("Completed", { exact: true })).toBeVisible();
  await expect(scores.getByText("4,321")).toBeVisible();
  await expect(scores.getByText(digest("2"))).toBeVisible();
  const evidence = page.getByRole("region", { name: "Attempt evidence" }).first();
  await expect(evidence.getByText(digest("8"))).toBeVisible();
  await expect(evidence.getByText("2 redacted paths: 1 secret, 1 sensitive, 0 prohibited")).toBeVisible();
  await evidence.getByRole("listitem").filter({ hasText: "Trace" }).getByRole("button", { name: "View" }).click();
  const viewer = page.getByLabel("Trace section");
  await expect(viewer).toContainText("[redacted]");
  const redactions = viewer.getByRole("list", { name: "Redacted paths" });
  await expect(redactions).toContainText("trace[0].api_key");
  await expect(redactions.getByText("Secret", { exact: true })).toBeVisible();
  await expect(redactions.getByText("Sensitive", { exact: true })).toBeVisible();
  await expect(redactions).toContainText(`policy ${digest("8").slice(0, 12)}`);
});

test("freezes a snapshot and exports and imports a replay bundle from the run page", async ({ page }) => {
  await mockRunPage(page);
  let frozen = false;
  await page.route("**/api/evaluation/runs/run-ops/analyses", async (route) => route.fulfill({ json: { run_id: "run-ops", snapshots: frozen ? [{ id: "snapshot-browser", record_checksum: digest("a"), created_at: now, superseded_by: null, supersession_reason: null, current: true }] : [] } }));
  await page.route("**/api/evaluation/runs/run-ops/reconciliations", async (route) => route.fulfill({ json: { run_id: "run-ops", reconciliations: [] } }));
  await page.route("**/api/evaluation/runs/run-ops/analyses/freeze", async (route, request) => {
    const body = request.postDataJSON() as { families: Record<string, string[]>; metric_ids: string[]; comparison_family: { comparisons: Array<{ baseline_arm: string; candidate_arm: string }> }; scorer_id: string };
    expect(body.families).toEqual({ arithmetic: ["item-1", "item-2"] });
    expect(body.metric_ids).toEqual(["metric-ops"]);
    expect(body.scorer_id).toBe("scorer-exact");
    expect(body.comparison_family.comparisons[0]).toMatchObject({ baseline_arm: "classic-a", candidate_arm: "classic-b" });
    frozen = true;
    return route.fulfill({ status: 201, json: { snapshot_id: "snapshot-browser", replay: { claim: "analysis_replayable" } } });
  });
  await page.route("**/api/evaluation/runs/run-ops/replay-bundles**", async (route, request) => {
    expect(new URL(request.url()).searchParams.get("policy")).toBe("redacted");
    return route.fulfill({ status: 201, json: bundleExport });
  });
  await page.route("**/api/evaluation/replay-bundles/import", async (route, request) => {
    const body = request.postDataJSON() as { archive_base64: string; approval?: { actor: string } };
    expect(body.archive_base64).toBe(bundleExport.archive_base64);
    expect(body.approval?.actor).toBe("journey-operator");
    return route.fulfill({ json: { import_id: digest("1"), ingestion_state: "readable", quarantined_members: [], stripped_fields: [], readable_members: ["records/run.json", "records/snapshot.json"], replay_approved: true, execution_repeat: "not_guaranteed_by_this_bundle", replay: { analysis_replayable: true, claim: "analysis_replayable", results_digest: digest("2"), expected_results_digest: digest("2"), execution_repeat: "not_guaranteed_by_this_bundle" } } });
  });

  await page.goto("/runs/run-ops");
  const history = page.getByRole("region", { name: "Analysis history" });
  await expect(history.getByText("0 stored snapshots")).toBeVisible();
  await history.getByRole("button", { name: "Freeze analysis" }).click();
  await page.getByRole("checkbox", { name: "metric-ops" }).check();
  await page.getByRole("button", { name: "Freeze snapshot" }).click();
  await expect(history.getByText("1 stored snapshots")).toBeVisible();
  await expect(history.getByText("Current", { exact: true })).toBeVisible();

  const bundle = page.getByRole("region", { name: "Replay bundle" });
  await bundle.getByRole("button", { name: "Export bundle" }).click();
  const manifest = page.getByLabel("Bundle manifest");
  await expect(manifest).toContainText(digest("0"));
  await expect(manifest.getByRole("row", { name: /Record 2/ })).toBeVisible();
  await expect(manifest.getByRole("row", { name: /Artifact 1 2.0 KiB/ })).toBeVisible();
  await expect(bundle.getByRole("button", { name: "Download archive" })).toBeVisible();
  await page.getByLabel("Bundle archive").setInputFiles({ name: "bundle.zip", mimeType: "application/zip", buffer: Buffer.from("PK archive") });
  await page.getByLabel("Approving actor").fill("journey-operator");
  await page.getByRole("button", { name: "Import bundle" }).click();
  const result = page.getByRole("status", { name: "Import result" });
  await expect(result).toContainText("Replay verified");
  await expect(result).toContainText("Approved");
});

test("authors, previews, and publishes a study", async ({ page }) => {
  const studies: Array<Record<string, unknown>> = [];
  await page.route("**/api/datasets", async (route) => route.fulfill({ json: { datasets: [{ id: "dataset-ops", name: "Operations data", description: "", source_uri: null, license: null, author: null, metadata: {}, created_at: now, updated_at: now, latest_version_id: "version-ops", latest_version: 1, latest_status: "published", latest_checksum: "abc", item_count: 2, latest_published_at: now, version_count: 1 }] } }));
  await page.route("**/api/datasets/dataset-ops/versions/version-ops/items**", async (route) => route.fulfill({ json: { items: [
    { id: "1", item_key: "item-1", input: "1+1", expected_output: "2", subject: "arithmetic", split: "test", tags: [] },
    { id: "2", item_key: "item-2", input: "2+2", expected_output: "4", subject: "arithmetic", split: "test", tags: [] },
  ], total: 2 } }));
  await page.route("**/api/evaluation/studies", async (route, request) => {
    if (request.method() === "POST") {
      const body = request.postDataJSON() as { publish: boolean; name: string; invariants: { case_ids: string[] }; families: Record<string, string[]>; scorer_versions: unknown[] };
      expect(body.invariants.case_ids).toEqual(["item-1", "item-2"]);
      expect(body.families).toEqual({ arithmetic: ["item-1", "item-2"] });
      if (body.publish) {
        expect(body.scorer_versions).toEqual([{ id: "scorer-exact", configuration: {} }]);
        studies.push({ study_id: "study-browser", study_type: "one_factor_ablation", run_plan_id: "plan-browser", test_revision_id: "revision-browser", record_checksum: digest("a"), created_at: now, record: { ...studyRecord, name: body.name } });
        return route.fulfill({ status: 201, json: { ...studyRecord, name: body.name, published: true, study_id: "study-browser", test_id: "test-browser", test_revision_id: "revision-browser", revision: 1, run_plan_id: "plan-browser" } });
      }
      return route.fulfill({ status: 201, json: { ...studyRecord, name: body.name, published: false } });
    }
    return route.fulfill({ json: { studies } });
  });

  await page.goto("/studies");
  await expect(page.getByText("No study")).toBeVisible();
  await page.getByRole("button", { name: "Author study" }).click();
  await page.getByLabel("Name", { exact: true }).fill("browser study");
  const datasetBox = page.getByRole("combobox", { name: "Dataset", exact: true });
  await datasetBox.focus();
  await page.keyboard.press("ArrowDown");
  await expect(datasetBox).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("option", { name: /Operations data/ }).dispatchEvent("click");
  await expect(page.getByLabel("Case ids (comma separated)")).toHaveValue("item-1, item-2");
  await expect(page.getByLabel("Dataset version id")).toHaveValue("version-ops");
  const scorerBox = page.getByRole("combobox", { name: "Scorer", exact: true });
  await scorerBox.focus();
  await page.keyboard.press("ArrowDown");
  await expect(scorerBox).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("option", { name: "Exact match" }).dispatchEvent("click");
  await page.getByRole("button", { name: "Preview estimates" }).click();
  const preview = page.getByRole("status", { name: "Study preview" });
  await expect(preview).toContainText("2 arms");
  await expect(preview).toContainText("0.02 USD");
  await expect(preview).toContainText("1.0 min at concurrency 4");
  await page.getByRole("button", { name: "Publish study" }).click();
  await expect(page.getByRole("link", { name: "browser study" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "revision-browser" })).toBeVisible();
});

test("lists anchor sets with their schedule and drift and calibrates one now", async ({ page }) => {
  let calibrated = false;
  await page.route("**/api/evaluation/judges/anchor-sets**", async (route, request) => {
    if (request.method() === "POST") return route.fulfill({ status: 201, json: { anchor_id: "anchor-new" } });
    return route.fulfill({ json: { anchor_sets: [{
      id: "anchor-a", judge_id: "judge-a", judge_version: "2", state: "active", next_due_at: calibrated ? "2026-09-11T12:00:00Z" : "2026-09-01T00:00:00Z", last_calibrated_at: calibrated ? now : null, created_at: now, due: !calibrated,
      record: { anchor_id: "anchor-a", judge: { judge_id: "judge-a", version: "2", model: "judge-model", prompt_digest: digest("a") }, scorer: { scorer_id: "scorer-exact", version: "1" }, label_set: { dataset_id: "dataset-ops", version: "1", items: [{ item_id: "item-1", label: "pass" }, { item_id: "item-2", label: "fail" }] }, candidate_models: [], schedule: { interval_days: 7, next_due_at: "2026-09-01T00:00:00Z", created_at: now }, threshold: 0.7, drift_tolerance: 0.1, state: "active" },
    }] } });
  });
  await page.route("**/api/evaluation/judges/judge-a/versions/2/calibration", async (route) => route.fulfill({ json: {
    calibration_id: "calibration-a", judge: { judge_id: "judge-a", version: "2", model: "judge-model", prompt_digest: digest("a") }, scorer: { scorer_id: "scorer-exact", version: "1" },
    dataset: { dataset_id: "dataset-ops", version: "1", label_digest: digest("b"), item_count: 12 }, agreement: { raw: 0.8333, kappa: 0.66, kappa_defined: true, interval: { low: 0.55, high: 0.95 } },
    disagreement: { count: 2, item_ids: [] }, invalid_output: { count: 0, rate: 0 }, abstention: { count: 1, rate: 0.0833 }, drift: { previous_version: "1", raw_agreement_delta: -0.15, exceeds_policy: true }, state: "current", threshold: 0.7, calibrated_at: now,
  } }));
  await page.route("**/api/evaluation/judges/anchor-sets/anchor-a/calibrate**", async (route) => {
    calibrated = true;
    return route.fulfill({ json: { anchor_id: "anchor-a", calibration_id: "calibration-b", state: "current", raw_agreement: 0.9167, next_due_at: "2026-09-11T12:00:00Z", judge_outputs: {} } });
  });

  await page.goto("/judges");
  const row = page.getByRole("row", { name: /anchor-a/ });
  await expect(row).toContainText("Overdue by");
  await expect(row).toContainText("83.3% raw");
  await expect(row).toContainText("kappa 0.66");
  await expect(row).toContainText("55.0% to 95.0%");
  await expect(row.getByText("Exceeds policy")).toBeVisible();
  await expect(row).toContainText("-15.0 pp against 1");
  await expect(row).toContainText("8.3% (1)");
  await row.getByRole("button", { name: "Calibrate now" }).click();
  await expect(page.getByText("91.7% raw agreement")).toBeVisible();
  await expect(page.getByRole("row", { name: /anchor-a/ })).toContainText("Due in");
});

test("shows the dataset version record with its digests, lineage, and splits", async ({ page }) => {
  await page.route("**/api/datasets/dataset-ops", async (route) => route.fulfill({ json: { dataset: {
    id: "dataset-ops", name: "Operations data", description: "", source_uri: null, license: null, author: null, metadata: {}, created_at: now, updated_at: now,
    versions: [{ id: "version-ops", dataset_id: "dataset-ops", version: 1, status: "published", checksum: "abcdef0123456789abcdef", item_count: 2, schema_json: { version: "1", source_format: "jsonl", mapping: {}, columns: [] }, source_filename: "ops.jsonl", source_mime: "application/x-ndjson", source_checksum: "abc", metadata: {}, created_at: now, published_at: now }],
    subjects: { arithmetic: 2 }, splits: { test: 2 },
  } } }));
  await page.route("**/api/datasets/dataset-ops/versions/version-ops/items**", async (route) => route.fulfill({ json: { items: [], total: 0 } }));
  await page.route("**/api/evaluation/datasets/dataset-ops/versions/version-ops/record", async (route) => route.fulfill({ json: {
    id: "version-ops", dataset_id: "dataset-ops", parent_version_id: null, content_digest: digest("e"), policy_digest: digest("a"), record_checksum: digest("f"), created_at: now,
    record: { version_id: "version-ops", parent_version_id: null, canonical_schema_version: "2", source_lineage: ["source-ops"], trust_inputs: [{ level: "owner_uploaded", policy_version: "1" }], effective_restrictions: [{ name: "deny_secrets", behavior: "hard" }],
      policy_digest: digest("a"), case_manifest_digest: digest("b"), transformation_recipe_digest: digest("c"), split_manifest: { test: ["item-1", "item-2"] }, asset_digests: [digest("d")], content_digest: digest("e"), validation_report_digest: digest("1"), contamination_record_digest: digest("2"), attribution_bundle_digest: digest("3") },
  } }));

  await page.goto("/datasets/dataset-ops");
  const record = page.getByRole("region", { name: "Version record" });
  await expect(record.getByText(digest("e"))).toBeVisible();
  await expect(record.getByText("None (first version)")).toBeVisible();
  await expect(record.getByLabel("Source lineage")).toContainText("source-ops");
  await expect(record.getByLabel("Source lineage")).toContainText("Deny secrets (hard)");
  await expect(record.getByLabel("Split manifest").getByRole("row", { name: /test 2 item-1, item-2/ })).toBeVisible();
  await expect(record.getByText("Asset digests (1)")).toBeVisible();
});
