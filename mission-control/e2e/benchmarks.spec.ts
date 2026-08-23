import { expect, test, type Page } from "@playwright/test";

const now = "2026-08-19T12:00:00Z";

async function mockShell(page: Page) {
  await page.route("**/api/tasks?**", async (route) => {
    await route.fulfill({
      json: { tasks: [], total: 0, grand_total: 0, limit: 50, offset: 0 },
    });
  });
  await page.route("**/api/stream/system", async (route) => {
    await route.fulfill({
      contentType: "text/event-stream",
      body: `event: daemon_status\ndata: ${JSON.stringify({ status: "ready", timestamp: now })}\n\n`,
    });
  });
}

test.beforeEach(async ({ page }) => {
  await mockShell(page);
});

test("shows every runtime and the current scheduler capacity", async ({ page }) => {
  await page.route("**/api/benchmarks/runtimes", async (route) => {
    await route.fulfill({
      json: {
        api_version: "1",
        variants: ["classic", "patchboard", "stigmergic"].map((id) => ({
          id,
          label: id === "classic" ? "Classic" : id === "patchboard" ? "Patchboard" : "Stigmergic workspace",
          available: true,
          contract_version: "1",
          configuration_schema_version: "1",
          supports_recovery: true,
          benchmark: {
            supported: true,
            configuration_schema: { type: "object", additionalProperties: false },
            seed_strategy: id === "classic" ? "applied" : "recorded",
            supports_repetitions: true,
            required_snapshot_fields: ["runtime_id", "runtime_configuration", "random_seed"],
          },
        })),
        qualifications: [],
        planned_runtime_ids: [],
      },
    });
  });
  await page.route("**/api/benchmarks/capacity", async (route) => {
    await route.fulfill({
      json: {
        schema_version: "1",
        global: { active: 2, limit: 8, available: 6 },
        resources: [{ key: "runtime:patchboard", active: 1, limit: 3, available: 2 }],
        unlimited_active_resources: [],
        queue: { total: 4, by_priority: [{ priority: 50, count: 4 }] },
        workers: [{
          worker_id: "worker-e2e",
          hostname: "scheduler-a",
          process_id: 42,
          status: "active",
          last_seen_at: now,
          stale: 0,
          owned_attempts: 2,
        }],
      },
    });
  });

  await page.goto("/runtimes");

  await expect(page.getByRole("heading", { name: "Runtime qualifications" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Scheduler capacity" })).toBeVisible();
  await expect(page.getByText("2 of 8 active")).toBeVisible();
  const runtimeCatalog = page.getByLabel("Available benchmark runtimes");
  await expect(runtimeCatalog.getByRole("heading", { name: "Classic" })).toBeVisible();
  await expect(runtimeCatalog.getByRole("heading", { name: "Patchboard" })).toBeVisible();
  await expect(runtimeCatalog.getByRole("heading", { name: "Stigmergic workspace" })).toBeVisible();
  await expect(page.getByText("scheduler-a")).toBeVisible();
  await expect(page.getByText("Planned runtime adapters")).toHaveCount(0);
});

test("shows deterministic analysis and saves one immutable human review", async ({ page }) => {
  let reviewSaved = false;
  const run = {
    id: "run-e2e",
    test_id: "test-e2e",
    test_name: "Reasoning quality",
    test_revision_id: "revision-e2e",
    revision: 3,
    status: "completed",
    total_trials: 2,
    completed_trials: 2,
    total_attempts: 2,
    completed_attempts: 2,
    total_cost_usd: 0.12,
    created_at: now,
    started_at: now,
    completed_at: now,
    attempts: [
      {
        id: "attempt-classic",
        trial_id: "trial-classic",
        arm_name: "Classic",
        item_key: "math-1",
        repeat_index: 0,
        retry_index: 0,
        status: "completed",
        task_id: "task-classic",
        failure_category: null,
        error_message: null,
        total_cost_usd: 0.05,
        total_tokens: 1000,
        duration_ms: 2200,
        result_summary: "42",
        subject: "math",
        split: "test",
        tags: ["reasoning"],
      },
      {
        id: "attempt-patchboard",
        trial_id: "trial-patchboard",
        arm_name: "Patchboard",
        item_key: "math-1",
        repeat_index: 0,
        retry_index: 0,
        status: "completed",
        task_id: "task-patchboard",
        failure_category: null,
        error_message: null,
        total_cost_usd: 0.07,
        total_tokens: 1300,
        duration_ms: 2600,
        result_summary: "42 with proof",
        subject: "math",
        split: "test",
        tags: ["reasoning"],
      },
    ],
    scores: [
      { id: "score-a", attempt_id: "attempt-classic", scorer_id: "exact", scorer_name: "Exact", scorer_version: "1", status: "scored", score: 0.7, passed: 1, explanation: "Mostly correct", evidence: {} },
      { id: "score-b", attempt_id: "attempt-patchboard", scorer_id: "exact", scorer_name: "Exact", scorer_version: "1", status: "scored", score: 0.9, passed: 1, explanation: "Correct", evidence: {} },
    ],
    get human_reviews() {
      return reviewSaved ? [{ id: "review-e2e", attempt_id: "attempt-classic", reviewer_id: "mission-control", score: 0.8, passed: 1, note: "Verified result", created_at: now }] : [];
    },
  };
  const metric = (mean: number) => ({ count: 1, mean, ci_low: null, ci_high: null, p50: mean, p95: mean });
  const arm = (id: string, name: string, slug: string, runtime: string, score: number) => ({
    arm_id: id,
    arm_name: name,
    arm_slug: slug,
    runtime_id: runtime,
    attempt_count: 1,
    completed_count: 1,
    failure_count: 0,
    failure_rate: 0,
    cost_usd: metric(runtime === "classic" ? 0.05 : 0.07),
    duration_ms: metric(runtime === "classic" ? 2200 : 2600),
    tokens: metric(runtime === "classic" ? 1000 : 1300),
    scorers: [{ ...metric(score), scorer_id: "exact", scorer_name: "Exact", scorer_version: "1", passed: 1, failed: 0, excluded: 0 }],
  });
  const report = {
    schema_version: "2",
    interval_method: "deterministic_bca_bootstrap_95",
    analysis: { version: "2", confidence_level: 0.95, interval_method: "deterministic_bca_bootstrap", bootstrap_resamples: 999, paired_test: "exact_two_sided_sign_test", multiple_comparison_method: "holm_bonferroni", family_alpha: 0.05, practical_difference: 0.01 },
    run: { id: "run-e2e", status: "completed", test_id: "test-e2e", test_revision_id: "revision-e2e", test_configuration_checksum: "test-checksum", dataset_id: "dataset-e2e", dataset_checksum: "dataset-checksum", execution_plan_checksum: "plan-checksum" },
    filters: { subject: "math" },
    latest_attempt_count: 2,
    prior_attempt_count: 0,
    arms: [arm("classic", "Classic", "classic", "classic", 0.7), arm("patchboard", "Patchboard", "patchboard", "patchboard", 0.9)],
    comparisons: [{
      left_arm_id: "classic",
      left_arm_name: "Classic",
      right_arm_id: "patchboard",
      right_arm_name: "Patchboard",
      left_arm_slug: "classic",
      right_arm_slug: "patchboard",
      matched_attempts: 1,
      scorers: [{ ...metric(0.2), scorer_id: "exact", wins: 1, ties: 0, losses: 0, direction: "right_minus_left", probability_of_superiority: 1, standardized_paired_effect: null, p_value_raw: 1, p_value_adjusted: 1, practical_difference: 0.01, classification: "insufficient_sample", sample_guidance: { method: "normal_approximation_80_power", practical_difference: 0.01, recommended_pairs: null, reason: "A variance estimate and a positive practical difference are required" } }],
    }],
    diagnostics: {
      error_categories: [],
      slices: [{ dimension: "subject", value: "math", attempt_count: 2, arms: [{ arm_id: "classic", arm_name: "Classic", attempt_count: 1, failure_rate: 0, scorers: [{ ...metric(0.7), scorer_id: "exact" }] }] }],
      item_differences: [],
      item_difference_count: 1,
      item_differences_truncated: false,
      human_review: { available: false, reviewed_attempt_count: 0, review_count: 0, reason: "No human reviews exist." },
      human_calibration: [],
      scorer_agreement: [],
    },
    warnings: ["One or more paired comparisons use fewer than five scored pairs"],
    complete: true,
    report_checksum: "abcdef0123456789",
  };

  await page.route("**/api/benchmarks/runs/run-e2e/report?**", async (route) => route.fulfill({ json: report }));
  await page.route("**/api/benchmarks/runs/run-e2e", async (route) => route.fulfill({ json: run }));
  await page.route("**/api/benchmarks/attempts/attempt-classic/reviews", async (route) => {
    expect(route.request().method()).toBe("POST");
    expect(route.request().headers()["x-idempotency-key"]).toBeTruthy();
    expect(await route.request().postDataJSON()).toEqual({ score: 0.8, passed: true, note: "Verified result" });
    reviewSaved = true;
    await route.fulfill({ status: 201, json: { id: "review-e2e" } });
  });

  await page.goto("/runs/run-e2e?subject=math");

  await expect(page.getByRole("heading", { name: "Reasoning quality" })).toBeVisible();
  await expect(page.getByText(/Analysis v2 uses 999 deterministic BCa resamples/)).toBeVisible();
  await expect(page.getByText("Insufficient sample")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Diagnosis" })).toBeVisible();
  const classicAttempt = page.getByRole("row", { name: /Classic math-1/ });
  await classicAttempt.locator("summary", { hasText: "Human review" }).click();
  const review = classicAttempt.locator("form");
  await review.getByLabel("Score from 0 to 1").fill("0.8");
  await review.getByLabel("Review note").fill("Verified result");
  await review.getByRole("button", { name: "Save immutable review" }).click();
  await expect(page.getByText("Verified result")).toBeVisible();
});

test("starts a benchmark run with the selected queue priority", async ({ page }) => {
  await page.route("**/api/benchmarks/tests/test-e2e", async (route) => route.fulfill({
    json: {
      id: "test-e2e",
      name: "Runtime comparison",
      description: "Compare stable runtime contracts.",
      revisions: [{
        id: "revision-e2e",
        revision: 1,
        dataset_version_id: "dataset-version-e2e",
        dataset_name: "Reasoning set",
        dataset_version: 2,
        item_count: 20,
        configuration: { repetitions: 2, max_concurrency: 4, timeout_seconds: 600 },
        configuration_checksum: "1234567890abcdef",
        published_at: now,
        arms: [{ id: "arm-e2e", name: "Patchboard", slug: "patchboard", runtime_id: "patchboard", configuration: {}, configuration_checksum: "fedcba0987654321" }],
        scorers: [],
        runs: [],
      }],
    },
  }));
  await page.route("**/api/benchmarks/tests/test-e2e/revisions/revision-e2e/runs", async (route) => {
    expect(await route.request().postDataJSON()).toEqual({ operator_note: "Started from Mission Control", priority: 100 });
    await route.fulfill({ status: 201, json: { id: "run-priority-e2e" } });
  });
  await page.route("**/api/benchmarks/runs/run-priority-e2e", async (route) => route.fulfill({ status: 404, json: { detail: "Not needed for this navigation test" } }));

  await page.goto("/tests/test-e2e");
  await page.getByLabel("Queue priority").click();
  await page.getByRole("option", { name: "Urgent" }).click();
  await page.getByRole("button", { name: "Start run" }).click();
  await expect(page).toHaveURL(/\/runs\/run-priority-e2e$/);
});
