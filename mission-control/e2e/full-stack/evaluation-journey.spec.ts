import { expect, test, type APIRequestContext } from "@playwright/test";
import { readStack } from "./stack";

/**
 * The unmocked evaluation journey against the complete local stack.
 *
 * Nothing in this file mocks the daemon, the database, Redis, the
 * scheduler, authentication, or admission. Every evaluation step
 * calls the real versioned API of the running daemon, and every
 * browser step reads the real Mission Control pages that proxy that
 * daemon. The deterministic fake provider answers every model call.
 */
type Json = Record<string, unknown>;
type JsonList = Array<Record<string, unknown>>;

const stack = readStack();
const headers = { Authorization: `Bearer ${stack.api_key}`, "X-API-Key": stack.api_key };

async function daemon(request: APIRequestContext, method: "get" | "post" | "put", path: string, data?: unknown) {
  const response = await request[method](`${stack.urls.daemon}${path}`, { headers, data });
  const body = await response.json().catch(() => ({}));
  expect(response.ok(), `${method.toUpperCase()} ${path}: ${JSON.stringify(body).slice(0, 400)}`).toBeTruthy();
  return body as Json;
}

function evaluationCase(caseId: string, instructions: string, answer: string) {
  return {
    schema_id: "evaluation-case",
    schema_version: 2,
    case_id: caseId,
    task: { instructions, messages: [], assets: [] },
    expected: { reference_answer: answer, final_state: null, rubric_id: null },
    environment: null,
    tools: [],
    limits: { max_tokens: 2048 },
    classification: { task_family: "arithmetic", split: "test", tags: ["journey"], intrinsic_horizon: null, human_minutes: 1 },
    contamination: {},
    metadata: {},
  };
}

test.describe.configure({ mode: "serial" });

test("every service answers a real readiness check", async ({ request }) => {
  for (const [name, url] of Object.entries(stack.urls)) {
    if (name === "redis") continue;
    const path = name === "mission_control" ? "/api/readiness" : "/health";
    const response = await request.get(`${url}${path}`);
    expect(response.status(), `${name} readiness`).toBeLessThan(400);
  }
  const readiness = await daemon(request, "get", "/readiness");
  expect((readiness.checks as Array<{ ready: boolean }>).every((check) => check.ready)).toBeTruthy();
});

test("the import, edit, publish, configure, execute, inspect, compare, export, and replay journey", async ({ page, request }) => {
  test.setTimeout(600_000);
  const suffix = Date.now().toString(36);

  // 1. Import a small public-style fixture through the local upload adapter.
  const fixture = [
    { id: "j-1", input: "What is 20 plus 22?", expected_output: "42", subject: "arithmetic", split: "test" },
    { id: "j-2", input: "What is 1 plus 2?", expected_output: "3", subject: "arithmetic", split: "test" },
    { id: "j-3", input: "What is 10 plus 5?", expected_output: "15", subject: "arithmetic", split: "test" },
  ].map((row) => JSON.stringify(row)).join("\n");
  const imported = await daemon(request, "post", "/api/evaluation/adapters/adapter-local-upload/import", {
    request: { filename: `journey-${suffix}.jsonl`, content_base64: Buffer.from(fixture).toString("base64") },
  });
  const sourceId = String(imported.source_id);
  expect(imported.item_count).toBe(3);

  // 2. Create and edit a draft: add cases, edit one, undo, redo.
  const draftId = `draft-journey-${suffix}`;
  await daemon(request, "post", "/api/evaluation/drafts", {
    record: {
      schema_id: "dataset-draft", schema_version: 2, draft_id: draftId,
      created_from: { kind: "source_import", reference: sourceId },
      source_ids: [sourceId], parent_version_id: null,
      trust_inputs: [{ level: "owner_uploaded", policy_version: "1" }],
      asset_policy: {}, effective_restrictions: [{ name: "deny_secrets", behavior: "hard" }],
      validation_issues: [], metadata: {},
    },
    links: { source_id: sourceId },
  });
  for (const [caseId, question, answer] of [["j-1", "What is 20 plus 22?", "42"], ["j-2", "What is 1 plus 2?", "3"], ["j-3", "What is 10 plus 5?", "15"]]) {
    await daemon(request, "put", `/api/evaluation/drafts/${draftId}/editor/cases`, { case: evaluationCase(caseId, question, answer) });
  }
  await daemon(request, "put", `/api/evaluation/drafts/${draftId}/editor/cases`, { case: evaluationCase("j-3", "What is 10 plus 6?", "16") });
  await daemon(request, "post", `/api/evaluation/drafts/${draftId}/editor/undo`);
  await daemon(request, "post", `/api/evaluation/drafts/${draftId}/editor/redo`);
  const validation = await daemon(request, "get", `/api/evaluation/drafts/${draftId}/validation`);
  expect(validation.issues).toEqual([]);
  const preview = await daemon(request, "get", `/api/evaluation/drafts/${draftId}/preview/distributions`);
  expect(preview.case_count).toBe(3);

  // 3. Publish the immutable dataset version under every governance rule.
  const datasetId = `dataset-journey-${suffix}`;
  const versionId = `version-journey-${suffix}`;
  const published = await daemon(request, "post", `/api/evaluation/drafts/${draftId}/publish-governed`, {
    dataset_id: datasetId, version_id: versionId, name: `Journey ${suffix}`,
  });
  expect(published.contamination_record_id).toBeTruthy();
  await page.goto("/datasets");
  await expect(page.getByText(`Journey ${suffix}`)).toBeVisible({ timeout: 30_000 });

  // 4. Create a test from the classic runtime preset.
  const runtimes = await daemon(request, "get", "/benchmarks/runtimes");
  const classic = (runtimes.variants as Array<{ id: string; available: boolean }>).find((variant) => variant.id === "classic");
  expect(classic?.available).toBeTruthy();
  const scorers = await daemon(request, "get", "/benchmarks/scorers");
  const scorerId = String((scorers.scorers as JsonList)[0].id);
  const testInput = {
    name: `Journey test ${suffix}`, description: "unmocked journey", dataset_version_id: versionId,
    repetitions: 1, seed: 7, max_concurrency: 2, timeout_seconds: 120,
    arms: [{ name: "Classic A", runtime_id: "classic", configuration: {} }],
    scorers: [{ id: scorerId, configuration: {}, required: true }],
  };
  // 5. Add a second arm and inspect the difference through preflight.
  const twoArms = { ...testInput, arms: [...testInput.arms, { name: "Classic B", runtime_id: "classic", configuration: {} }] };
  const preflight = await daemon(request, "post", "/benchmarks/tests/preflight", twoArms);
  expect(preflight.valid).toBeTruthy();
  expect(preflight.total_attempts).toBe(6);
  const created = await daemon(request, "post", "/benchmarks/tests", twoArms);
  const testId = String(created.test_id ?? created.id);
  const revisions = (created.revisions ?? []) as JsonList;
  const revisionId = revisions.length ? String(revisions[revisions.length - 1].id) : String(created.latest_revision_id ?? created.revision_id ?? "");
  expect(revisionId, `revision id from ${JSON.stringify(Object.keys(created))}`).toBeTruthy();
  await page.goto("/tests");
  await expect(page.getByText(`Journey test ${suffix}`)).toBeVisible({ timeout: 30_000 });

  // 6-7. Preflight passed; start the deterministic run.
  const run = await daemon(request, "post", `/benchmarks/tests/${testId}/revisions/${revisionId}/runs`, { operator_note: "journey", priority: 10 });
  const runId = String(run.id);
  await page.goto("/runs");
  await expect(page.getByText(`Journey test ${suffix}`)).toBeVisible({ timeout: 30_000 });

  // 8-9. Inspect the live run until every attempt reaches a terminal state.
  let detail: Json = {};
  await expect.poll(async () => {
    detail = await daemon(request, "get", `/benchmarks/runs/${runId}`);
    return String(detail.status);
  }, { timeout: 420_000, intervals: [2_000] }).toMatch(/completed|partial|failed|cancelled/);
  const attempts = (detail.attempts ?? []) as JsonList;
  expect(detail.status, JSON.stringify(attempts.map((a) => [a.status, a.failure_category, a.error_message]))).toBe("completed");
  expect(attempts.length).toBe(6);
  expect(attempts.every((attempt) => attempt.status === "completed")).toBeTruthy();
  await page.goto(`/runs/${runId}`);
  await expect(page.getByText(`Journey test ${suffix}`)).toBeVisible({ timeout: 30_000 });

  // 10. Review paired results, denominators, and cost in the report.
  const report = await daemon(request, "get", `/benchmarks/runs/${runId}/report`);
  expect((report.analysis as Json)?.estimand).toBeTruthy();
  expect((report.denominators as Json)?.planned).toBe(6);
  expect(Array.isArray(report.comparisons)).toBeTruthy();

  // 11. Create a compatible baseline gate on the completed run and
  // preview the candidate against it.
  const baseline = await daemon(request, "post", "/benchmarks/baselines", {
    run_id: runId, name: `Journey baseline ${suffix}`, description: "gate",
    rules: [
      { id: "cost", label: "Cost stays bounded", metric: "arm.classic-a.cost_usd.mean", operator: "lte", value: 100 },
      // The frozen rule decides through the frozen non-inferiority
      // engine instead of the legacy report engine.
      { id: "frozen", label: "Success stays within the margin", metric: `frozen.${scorerId}`, operator: "max_drop", value: 0.2,
        analysis_method: "frozen_non_inferiority", direction: "improvement", resample_count: 199 },
    ],
  });
  const baselineId = String(baseline.id ?? baseline.baseline_id);
  expect(baselineId).toBeTruthy();
  const previewed = await daemon(request, "post", `/benchmarks/baselines/${baselineId}/preview`, { candidate_run_id: runId });
  expect(previewed.saved).toBe(false);
  const previewReport = previewed.report as Json;
  expect(previewReport?.status).toBeTruthy();
  const previewRules = (previewReport.rules ?? []) as JsonList;
  const frozenRule = previewRules.find((rule) => rule.id === "frozen") as Json;
  expect(frozenRule, JSON.stringify(previewRules)).toBeTruthy();
  expect((frozenRule.frozen as Json)?.engine).toBe("bmas-frozen-analysis");
  expect(["passed", "failed", "indeterminate"]).toContain(String(frozenRule.status));
  expect(previewReport.engines).toContain("bmas-frozen-analysis");
  await page.goto("/baselines");
  await expect(page.getByText(`Journey baseline ${suffix}`)).toBeVisible({ timeout: 30_000 });
  await page.goto(`/baselines/${baselineId}`);
  await expect(page.getByRole("cell", { name: /Frozen non-inferiority · margin \+20.0 pp · higher is better/ })).toBeVisible({ timeout: 30_000 });
  const candidateBox = page.getByRole("combobox", { name: "Candidate run", exact: true });
  await candidateBox.focus();
  await page.keyboard.press("ArrowDown");
  await expect(candidateBox).toHaveAttribute("aria-expanded", "true");
  // The menu closes on any ancestor scroll, so a dispatched click
  // commits the option without a scroll-into-view step.
  await page.getByRole("option").filter({ hasNotText: "Select a run" }).first().dispatchEvent("click");
  await expect(page.getByRole("button", { name: "Preview gate" })).toBeEnabled();
  await page.getByRole("button", { name: "Preview gate" }).click();
  const browserPreview = page.getByRole("region", { name: "Unsaved preview" });
  await expect(browserPreview).toContainText("bmas-frozen-analysis", { timeout: 60_000 });

  // 12. Publish the metric definition every displayed metric resolves
  // to, freeze the analysis, serve the frozen report, export the
  // replay bundle, and reimport it.
  const metricId = `metric-journey-${suffix}`;
  const digest = "a".repeat(64);
  await daemon(request, "post", "/api/evaluation/metrics", {
    record: {
      schema_id: "metric-definition", schema_version: 2, metric_id: metricId, lifecycle_state: "draft",
      calibration: { state: "current", method: "deterministic", version: "1", dataset: "calibration-fixtures",
        result: { limits_failed: false, pinned_digests: {} }, calibrated_at: "2026-09-01T00:00:00Z",
        expires_at: "2027-09-01T00:00:00Z", drift_policy: "recalibrate-on-implementation-change" },
      population: { target: "declared dataset cases", inclusion_rule: "Every planned non-excluded slot counts." },
      measurement: { numerator: "Cases with a passing binary reduction.", denominator: "Unconditional planned cases.", unit: "proportion",
        range: { minimum: 0, maximum: 1 }, direction: "higher_is_better", aggregation: "family_stratified_weighted_mean" },
      labels: { source: "scorer", evidence_contract: ["final_output"] },
      scorer: { scorer_id: scorerId, version: "1", configuration_digest: digest },
      missingness: "predeclared_infrastructure_exclusions", exclusions: [],
      uncertainty_method: "family_stratified_weighted_case_bootstrap",
    },
  });
  await daemon(request, "post", `/api/evaluation/metrics/${metricId}/advance`, {
    target: "validated", now: "2026-09-03T00:00:00Z", validation_evidence: { schema: true, fixture: true, evidence: true },
  });
  await daemon(request, "post", `/api/evaluation/metrics/${metricId}/advance`, { target: "published", now: "2026-09-03T00:00:00Z" });
  const frozen = await daemon(request, "post", `/api/evaluation/runs/${runId}/analyses/freeze`, {
    families: { arithmetic: attempts.map((attempt) => String(attempt.dataset_item_id)).filter((v, i, a) => a.indexOf(v) === i) },
    scorer_id: scorerId, master_seed: 7, planned_repetitions: 1, resample_count: 99, metric_ids: [metricId],
    comparison_family: { family_id: "journey", comparisons: [{ comparison_id: "a-vs-b", baseline_arm: attempts[0].arm_id, candidate_arm: attempts[attempts.length - 1].arm_id, non_inferiority_margin: 0.2 }] },
  });
  expect((frozen.replay as Json).claim).toBe("analysis_replayable");
  const frozenReport = await daemon(request, "get", `/benchmarks/runs/${runId}/report`);
  expect(frozenReport.engine).toBe("bmas-frozen-analysis");
  expect(frozenReport.replay_verified).toBe(true);
  expect((frozenReport.metrics as JsonList).map((metric) => metric.metric_id)).toEqual([metricId]);
  expect(frozenReport.unresolved_metrics).toEqual([]);
  expect((frozenReport.denominators as Json)?.planned).toBe(6);
  await page.goto(`/runs/${runId}`);
  const reportRegion = page.getByRole("region", { name: "Comparison report" });
  await expect(reportRegion.locator(".frozen-report__badges").getByText("Frozen snapshot", { exact: true })).toBeVisible({ timeout: 30_000 });
  await expect(reportRegion.getByText("Replay verified")).toBeVisible();
  await expect(reportRegion.getByRole("link", { name: metricId })).toBeVisible();
  const historyRegion = page.getByRole("region", { name: "Analysis history" });
  await expect(historyRegion.getByText("1 stored snapshots")).toBeVisible({ timeout: 30_000 });
  await expect(historyRegion.getByText("Current", { exact: true })).toBeVisible();
  await page.goto("/metrics");
  await expect(page.getByRole("link", { name: metricId })).toBeVisible({ timeout: 30_000 });
  await expect(page.getByRole("cell", { name: "Published" })).toBeVisible();
  await page.goto(`/metrics/${metricId}`);
  await expect(page.getByRole("button", { name: "Deprecate" })).toBeVisible({ timeout: 30_000 });
  const exported = await daemon(request, "post", `/api/evaluation/runs/${runId}/replay-bundles?policy=redacted`);
  expect(exported.member_count).toBeGreaterThan(10);
  const reimported = await daemon(request, "post", "/api/evaluation/replay-bundles/import", {
    archive_base64: exported.archive_base64, approval: { actor: "journey-operator", policy_version: "1" },
  });
  expect(reimported.replay_approved).toBeTruthy();
  expect((reimported.replay as Json).analysis_replayable).toBeTruthy();
  expect((reimported.replay as Json).execution_repeat).toBe("not_guaranteed_by_this_bundle");
});
