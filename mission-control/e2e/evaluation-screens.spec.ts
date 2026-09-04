import { expect, test, type Page } from "@playwright/test";

const now = "2026-09-03T12:00:00Z";
const digest = "a".repeat(64);

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
}

const run = {
  id: "run-frozen",
  test_id: "test-frozen",
  test_name: "Frozen journey",
  test_revision_id: "revision-frozen",
  revision: 1,
  status: "completed",
  total_trials: 2,
  completed_trials: 2,
  total_attempts: 2,
  completed_attempts: 2,
  total_cost_usd: 0.1,
  created_at: now,
  started_at: now,
  completed_at: now,
  attempts: [],
  scores: [],
  human_reviews: [],
};

const frozenComparison = {
  comparison_id: "a-vs-b",
  metric: "exact",
  baseline_arm: "classic-a",
  candidate_arm: "classic-b",
  direction: "higher_is_better",
  hypothesis: "non_inferiority",
  non_inferiority_margin: 0.2,
  minimum_usable_cases: 1,
  estimate: 0.05,
  interval: { status: "estimated", low: -0.1, high: 0.2, method: "family_stratified_weighted_case_bootstrap_percentile", unit: "case", replicate_count: 99 },
  test: { method: "paired_sign_flip", mode: "monte_carlo", p_value: 0.42, resamples: 99 },
  p_value_adjusted: 0.42,
  counts: { paired_cases: 3, missing_cases: 0, removed_slots: 0 },
  limit_failures: [],
  primary_valid: true,
  small_families: [],
  comparative_claim: true,
  statistical_unit: "case",
  gate: { status: "passed", reasons: [], bound: -0.1, margin: 0.2, rule: "lower_bound_above_negative_margin" },
};

const metricDefinition = {
  metric_id: "metric-journey",
  calibration_version: "1",
  lifecycle_state: "published",
  scorer: { scorer_id: "exact", version: "1", configuration_digest: digest },
  measurement: { numerator: "Cases with a passing binary reduction.", denominator: "Unconditional planned cases.", unit: "proportion", range: { minimum: 0, maximum: 1 }, direction: "higher_is_better", aggregation: "family_stratified_weighted_mean" },
};

const frozenReport = {
  engine: "bmas-frozen-analysis",
  engine_version: "1",
  snapshot_id: "snapshot-current-000001",
  replay_verified: true,
  results_digest: "b".repeat(64),
  stored_results_digest: "b".repeat(64),
  metrics: [metricDefinition],
  unresolved_metrics: [],
  analysis: { estimand: "family-balanced-unconditional-task-success", statistical_unit: "case", specification_digest: "c".repeat(64), replay_claim: "analysis_replayable" },
  denominators: { planned: 6, statement: "planned slots minus predeclared infrastructure exclusions" },
  comparisons: [frozenComparison],
  arms: {
    "classic-a": { counts: { planned: 3, admitted: 3, failed: 0, retried: 0, missing: 0, excluded: 0, observed: 3 }, unconditional_denominator: 3, unconditional_successes: 2, unconditional_success_rate: 0.6667, denominator_statement: "planned slots minus predeclared infrastructure exclusions", latency_ms: { count: 0, median_ms: null, p95_ms: null } },
    "classic-b": { counts: { planned: 3, admitted: 3, failed: 0, retried: 0, missing: 0, excluded: 0, observed: 3 }, unconditional_denominator: 3, unconditional_successes: 3, unconditional_success_rate: 1, denominator_statement: "planned slots minus predeclared infrastructure exclusions", latency_ms: { count: 0, median_ms: null, p95_ms: null } },
  },
  resources: { available: true, currency: "USD", actual_total: { currency: "USD", amount_nanos: 400000000 }, estimate_total: { currency: "USD", amount_nanos: 0 }, unknown_entry_ids: [], cost_per_success: { currency: "USD", amount_nanos: 80000000 }, unconditional_successes: 5 },
  warnings: [],
  report: { metric_ids: ["metric-journey"], results_digest: "b".repeat(64), input_digest: "d".repeat(64) },
};

function overview(estimate: number, gate: string, costNanos: number | null) {
  return {
    sections: [
      { view: "success_funnel" },
      { view: "primary_metric_with_uncertainty", rows: [{ comparison_id: "a-vs-b", estimate, interval_low: estimate - 0.1, interval_high: estimate + 0.1, interval_status: "estimated", unit: "case", method: "bootstrap", p_value_adjusted: 0.3, gate, primary_valid: true }] },
    ],
    estimand: "family-balanced-unconditional-task-success",
    replay: { claim: "analysis_replayable" },
    resources: costNanos === null ? { available: false, statement: "no resource ledger" } : { available: true, currency: "USD", actual_total: { currency: "USD", amount_nanos: costNanos }, cost_per_success: { currency: "USD", amount_nanos: costNanos / 2 }, unconditional_successes: 2 },
  };
}

// The select menu closes on any window scroll, so the viewport stays
// tall enough that every option list fits without scrolling.
test.use({ viewport: { width: 1280, height: 1800 } });

test.beforeEach(async ({ page }) => {
  await mockShell(page);
});

test("renders the frozen report with resolved metrics, denominators, and decisions", async ({ page }) => {
  await page.route("**/api/benchmarks/runs/run-frozen", async (route) => route.fulfill({ json: run }));
  await page.route("**/api/benchmarks/runs/run-frozen/report**", async (route) => route.fulfill({ json: frozenReport }));
  await page.route("**/api/evaluation/runs/run-frozen/analyses", async (route) => route.fulfill({ json: { run_id: "run-frozen", snapshots: [
    { id: "snapshot-old-000000", record_checksum: "e".repeat(64), created_at: "2026-09-01T00:00:00Z", superseded_by: "snapshot-current-000001", supersession_reason: "late_charge_changed_cost_rule", current: false },
    { id: "snapshot-current-000001", record_checksum: "f".repeat(64), created_at: "2026-09-02T00:00:00Z", superseded_by: null, supersession_reason: null, current: true },
  ] } }));
  await page.route("**/api/evaluation/runs/run-frozen/analyses/snapshot-old-000000/overview", async (route) => route.fulfill({ json: overview(0.05, "passed", null) }));
  await page.route("**/api/evaluation/runs/run-frozen/analyses/snapshot-current-000001/overview", async (route) => route.fulfill({ json: overview(0.05, "passed", 400000000) }));

  await page.goto("/runs/run-frozen");
  const report = page.getByRole("region", { name: "Comparison report" });
  await expect(report.locator(".frozen-report__badges").getByText("Frozen snapshot", { exact: true })).toBeVisible();
  await expect(report.getByText("Replay verified")).toBeVisible();
  await expect(report.getByText("Metrics published")).toBeVisible();
  await expect(report.getByRole("heading", { name: "Denominators" })).toBeVisible();
  await expect(report.getByText("6", { exact: true }).first()).toBeVisible();
  await expect(report.getByRole("link", { name: "metric-journey" })).toBeVisible();
  await expect(report.getByRole("img", { name: /a-vs-b: interval from -10.0 to 20.0 percentage points, margin at -20.0 points/ })).toBeVisible();
  await expect(report.getByText("Passed", { exact: true })).toBeVisible();
  await expect(report.getByText("+5.0 pp", { exact: true })).toBeVisible();
  await expect(report.getByText(/Cost per success 0.08 USD/)).toBeVisible();

  const history = page.getByRole("region", { name: "Analysis history" });
  await expect(history.getByText("2 stored snapshots")).toBeVisible();
  await expect(history.getByText("Superseded", { exact: true })).toBeVisible();
  await expect(history.getByText("Late charge changed cost rule")).toBeVisible();
  await history.getByRole("button", { name: "Compare with successor" }).click();
  const compare = page.getByRole("region", { name: /Superseded .* beside current/ });
  await expect(compare.getByRole("cell", { name: "0.4 USD" })).toBeVisible();
  await expect(compare.getByText("changed", { exact: true }).first()).toBeVisible();
});

test("blocks the frozen report until a metric definition is published and offers the legacy engine", async ({ page }) => {
  await page.route("**/api/benchmarks/runs/run-frozen", async (route) => route.fulfill({ json: run }));
  await page.route("**/api/benchmarks/runs/run-frozen/report**", async (route) => {
    const url = new URL(route.request().url());
    if (url.searchParams.get("allow_unresolved") === "true") {
      await route.fulfill({ json: { ...frozenReport, metrics: [], unresolved_metrics: [{ metric_id: "metric-journey", reason: "The displayed metric metric-journey has no registered definition" }] } });
      return;
    }
    await route.fulfill({ status: 409, json: { detail: "Every displayed metric references one published definition; unresolved: metric-journey: no registered definition" } });
  });
  await page.route("**/api/evaluation/runs/run-frozen/analyses", async (route) => route.fulfill({ json: { run_id: "run-frozen", snapshots: [] } }));

  await page.goto("/runs/run-frozen");
  const report = page.getByRole("region", { name: "Comparison report" });
  await expect(report.getByRole("alert")).toContainText("blocked until every displayed metric resolves to a published definition");
  await expect(report.getByRole("link", { name: "Register and publish the metric definition" })).toHaveAttribute("href", "/metrics");
  await report.getByLabel(/Show the report while a metric definition is unpublished/).click();
  await expect(page).toHaveURL(/allow_unresolved=true/);
  await expect(report.getByText("Metrics unresolved")).toBeVisible({ timeout: 15000 });
  await expect(report.getByText("No metric declared").or(report.getByText("metric-journey", { exact: true }).first())).toBeVisible();
  await expect(page.getByRole("region", { name: "Analysis history" }).getByText("No frozen analysis")).toBeVisible();
});

test("authors a frozen non-inferiority rule with its margin, direction, and resamples", async ({ page }) => {
  const legacyReport = {
    complete: true, latest_attempt_count: 2, prior_attempt_count: 0, report_checksum: "9".repeat(64), warnings: [],
    analysis: { version: "2", bootstrap_resamples: 999 },
    arms: [{ arm_id: "arm-a", arm_name: "Classic A", arm_slug: "classic-a", runtime_id: "classic", attempt_count: 1, completed_count: 1, failure_count: 0, failure_rate: 0, cost_usd: { count: 1, mean: 0.01, ci_low: null, ci_high: null }, duration_ms: { count: 1, mean: 10, p95: 10, ci_low: null, ci_high: null }, tokens: { count: 1, mean: 5, ci_low: null, ci_high: null }, scorers: [{ scorer_id: "exact", scorer_name: "Exact", scorer_version: "1", count: 1, mean: 1, ci_low: null, ci_high: null, passed: 1, failed: 0, excluded: 0 }] }],
    comparisons: [],
    diagnostics: { human_review: { available: false, reason: "none", reviewed_attempt_count: 0, review_count: 0 }, item_difference_count: 0, item_differences_truncated: false, slices: [], error_categories: [], human_calibration: [] },
  };
  let posted: Record<string, unknown> | null = null;
  await page.route("**/api/benchmarks/baselines", async (route) => {
    if (route.request().method() === "POST") {
      posted = route.request().postDataJSON() as Record<string, unknown>;
      await route.fulfill({ status: 201, json: { id: "baseline-frozen" } });
      return;
    }
    await route.fulfill({ json: { baselines: [] } });
  });
  await page.route("**/api/benchmarks/runs?**", async (route) => route.fulfill({ json: { runs: [{ ...run, test_name: "Frozen journey" }] } }));
  await page.route("**/api/benchmarks/runs/run-frozen/report**", async (route) => route.fulfill({ json: legacyReport }));

  await page.goto("/baselines");
  await page.getByRole("button", { name: "Pin baseline" }).click();
  const reportLoaded = page.waitForResponse((response) => response.url().includes("/api/benchmarks/runs/run-frozen/report"));
  await page.getByLabel("Completed run").click();
  await page.getByRole("option", { name: /Frozen journey revision 1/ }).click();
  await reportLoaded;
  // The metric options render on the next React commit after the
  // report lands; one settle keeps the single click from racing it.
  await page.waitForTimeout(500);
  await page.getByLabel("Baseline name").fill("Frozen gate");
  await page.getByLabel("Label").fill("Success stays within the margin");
  // The menu closes on any ancestor scroll, so the keyboard opens and
  // walks the list instead of a pointer click that scrolls the option
  // into view. The options render once the report state lands, so the
  // poll reopens until the frozen option is present.
  const metricBox = page.getByRole("combobox", { name: "Metric" });
  const frozenLabel = "Frozen Exact across runs (first arm)";
  let target = -1;
  await expect.poll(async () => {
    await metricBox.focus();
    if (await metricBox.getAttribute("aria-expanded") !== "true") await page.keyboard.press("ArrowDown");
    const names = await page.getByRole("option").allTextContents();
    target = names.findIndex((name) => name.trim() === frozenLabel);
    if (target < 0) await page.keyboard.press("Escape");
    return target;
  }, { timeout: 15000 }).toBeGreaterThanOrEqual(0);
  // A dispatched click commits the option without the scroll-into-view
  // step of a pointer click.
  await page.getByRole("option", { name: frozenLabel }).dispatchEvent("click");
  await expect(metricBox).toContainText(frozenLabel);
  await expect(page.getByRole("combobox", { name: "Measure", exact: true })).toContainText("Frozen non-inferiority");
  await expect(page.getByRole("combobox", { name: "Rule", exact: true })).toContainText("Maximum drop (margin)");
  await page.getByLabel("Margin (0 to 1)").fill("0.2");
  await page.getByLabel("Bootstrap resamples").fill("199");
  const direction = page.getByRole("combobox", { name: "Direction", exact: true });
  await direction.focus();
  await page.keyboard.press("ArrowDown");
  await expect(direction).toHaveAttribute("aria-expanded", "true");
  await page.getByRole("option", { name: "Lower is better" }).dispatchEvent("click");
  await expect(direction).toContainText("Lower is better");
  await page.getByRole("button", { name: "Pin immutable baseline" }).click();
  await expect.poll(() => posted).not.toBeNull();
  const rules = (posted as unknown as { rules: Array<Record<string, unknown>> }).rules;
  expect(rules[0]).toEqual({ id: "rule-1", label: "Success stays within the margin", metric: "frozen.exact", operator: "max_drop", value: 0.2, analysis_method: "frozen_non_inferiority", direction: "reduction", resample_count: 199, minimum_usable_cases: 1 });
});

test("previews a gate and renders the frozen decision beside the legacy rule", async ({ page }) => {
  const baseline = {
    id: "baseline-frozen", test_id: "test-frozen", test_name: "Frozen journey", run_id: "run-frozen", run_status: "completed", name: "Frozen gate", description: "gate", created_by: "operator", created_at: now, rules_checksum: digest,
    rules: [
      { id: "cost", label: "Cost stays bounded", metric: "arm.classic-a.cost_usd.mean", operator: "lte", value: 100, analysis_method: "point_estimate" },
      { id: "frozen", label: "Success stays within the margin", metric: "frozen.exact", operator: "max_drop", value: 0.2, analysis_method: "frozen_non_inferiority", direction: "improvement", resample_count: 199, minimum_usable_cases: 1 },
    ],
    evaluations: [],
  };
  const gateReport = {
    status: "passed", reason: "Every regression rule passed", mode: "preview", baseline_run_id: "run-frozen", candidate_run_id: "run-candidate", report_checksum: "8".repeat(64), engines: ["bmas-frozen-analysis", "legacy-report"],
    rules: [
      { id: "cost", label: "Cost stays bounded", metric: "arm.classic-a.cost_usd.mean", operator: "lte", value: 100, analysis_method: "point_estimate", threshold: 100, baseline_value: 0.01, candidate_value: 0.02, boundary: 100, status: "passed" },
      { id: "frozen", label: "Success stays within the margin", metric: "frozen.exact", operator: "max_drop", value: 0.2, analysis_method: "frozen_non_inferiority", direction: "improvement", threshold: 0.2, baseline_value: null, candidate_value: 0.05, boundary: -0.1, status: "passed", frozen: { engine: "bmas-frozen-analysis", engine_version: "1", estimate: 0.05, interval: frozenComparison.interval, test: frozenComparison.test, p_value_adjusted: 0.42, gate: frozenComparison.gate, counts: frozenComparison.counts, baseline_arm: "classic-a", candidate_arm: "classic-a", statistical_unit: "case" } },
    ],
  };
  await page.route("**/api/benchmarks/baselines/baseline-frozen", async (route) => route.fulfill({ json: baseline }));
  await page.route("**/api/benchmarks/runs?**", async (route) => route.fulfill({ json: { runs: [{ ...run, id: "run-candidate", test_id: "test-frozen" }] } }));
  await page.route("**/api/benchmarks/baselines/baseline-frozen/preview", async (route) => {
    expect(route.request().postDataJSON()).toEqual({ candidate_run_id: "run-candidate" });
    await route.fulfill({ json: { report: gateReport, saved: false } });
  });

  await page.goto("/baselines/baseline-frozen");
  await expect(page.getByRole("cell", { name: /Frozen non-inferiority · margin \+20.0 pp · higher is better/ })).toBeVisible();
  await expect(page.getByRole("cell", { name: "199 resamples · at least 1 usable cases" })).toBeVisible();
  await page.getByLabel("Candidate run").click();
  await page.getByRole("option", { name: /Completed/ }).click();
  await page.getByRole("button", { name: "Preview gate" }).click();
  const preview = page.getByRole("region", { name: "Unsaved preview" });
  await expect(preview).toContainText("engines: bmas-frozen-analysis, legacy-report");
  await expect(preview.getByRole("img", { name: /Success stays within the margin: interval from -10.0 to 20.0 percentage points/ })).toBeVisible();
  await expect(preview).toContainText("bound -10.0 pp");
  await expect(preview).toContainText("the lower bound stays above the negative margin");
});

test("registers, validates, and publishes a metric definition", async ({ page }) => {
  let state: "draft" | "validated" | "published" = "draft";
  const advances: Array<Record<string, unknown>> = [];
  const storedRecord = () => ({
    metric_id: "metric-journey",
    lifecycle_state: state,
    calibration_state: "current",
    record_checksum: "7".repeat(64),
    created_at: now,
    record: {
      schema_id: "metric-definition", schema_version: 2, metric_id: "metric-journey", lifecycle_state: state,
      calibration: { state: "current", method: "deterministic", version: "1", dataset: "calibration-fixtures", result: { limits_failed: false, pinned_digests: {} }, calibrated_at: now, expires_at: "2027-09-03T12:00:00Z", drift_policy: "recalibrate-on-implementation-change" },
      population: { target: "declared dataset cases", inclusion_rule: "Every planned non-excluded slot counts." },
      measurement: metricDefinition.measurement,
      labels: { source: "scorer", evidence_contract: ["final_output"] },
      scorer: metricDefinition.scorer,
      missingness: "predeclared_infrastructure_exclusions",
      exclusions: [],
      uncertainty_method: "family_stratified_weighted_case_bootstrap",
    },
  });
  let registered = false;
  await page.route("**/api/evaluation/metrics", async (route) => {
    if (route.request().method() === "POST") {
      const body = route.request().postDataJSON() as { record: Record<string, unknown> };
      expect(body.record).toMatchObject({ schema_id: "metric-definition", metric_id: "metric-journey", lifecycle_state: "draft" });
      expect((body.record.calibration as Record<string, unknown>).drift_policy).toBe("recalibrate-on-implementation-change");
      registered = true;
      await route.fulfill({ status: 201, json: { id: "metric-journey" } });
      return;
    }
    await route.fulfill({ json: { metrics: registered ? [storedRecord()] : [] } });
  });
  await page.route("**/api/benchmarks/scorers", async (route) => route.fulfill({ json: { scorers: [{ id: "exact", name: "Exact", version: "1" }] } }));
  await page.route("**/api/evaluation/metrics/metric-journey", async (route) => route.fulfill({ json: storedRecord() }));
  await page.route("**/api/evaluation/metrics/metric-journey/advance", async (route) => {
    const body = route.request().postDataJSON() as { target: "validated" | "published" };
    advances.push(body);
    state = body.target;
    await route.fulfill({ json: { ...storedRecord().record, lifecycle_state: state } });
  });

  await page.goto("/metrics");
  await expect(page.getByText("No metric definitions")).toBeVisible();
  await page.getByRole("button", { name: "Register definition" }).click();
  await page.getByLabel("Metric id").fill("metric-journey");
  await page.getByRole("combobox", { name: "Scorer", exact: true }).click();
  await page.getByRole("option", { name: "Exact" }).click();
  await page.getByRole("button", { name: "Register draft definition" }).click();
  await expect(page.getByRole("link", { name: "metric-journey" })).toBeVisible();
  await expect(page.getByRole("cell", { name: "Draft" })).toBeVisible();

  await page.getByRole("link", { name: "metric-journey" }).click();
  await expect(page).toHaveURL(/\/metrics\/metric-journey$/);
  const steps = page.getByRole("list", { name: "Lifecycle steps" });
  await expect(steps.getByText("Draft")).toBeVisible();
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByRole("button", { name: "Publish" })).toBeVisible();
  await page.getByRole("button", { name: "Publish" }).click();
  await expect(page.getByRole("button", { name: "Deprecate" })).toBeVisible();
  expect(advances).toEqual([
    expect.objectContaining({ target: "validated", validation_evidence: { schema: true, fixture: true, evidence: true } }),
    expect.objectContaining({ target: "published" }),
  ]);
  await expect(page.getByText("Complete", { exact: true })).toBeVisible();
});
