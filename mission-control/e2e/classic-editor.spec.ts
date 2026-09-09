import { expect, test, type Page } from "@playwright/test";
import { classicShell, compileResponse } from "./classic-preview";

async function openTaskEditor(page: Page) {
  await page.goto("/");
  await page.getByRole("combobox", { name: "Runtime", exact: true }).click();
  await page.getByRole("option", { name: "Classic native · test only" }).click();
  await expect(page.getByTestId("classic-spec-digest")).toBeVisible();
  return page.getByRole("region", { name: "Classic specification", exact: true });
}

test.beforeEach(async ({ page }) => {
  await classicShell(page);
  await page.route("**/api/classic/spec", async (route) => {
    const response = await compileResponse(route.request().method(), route.request().postData() ?? "{}");
    await route.fulfill({ status: response.status, json: response.body });
  });
});

test("beginner completes a preview without raw JSON and sees every disclosure", async ({ page }, testInfo) => {
  const editor = await openTaskEditor(page);
  await editor.getByLabel("Effort profile", { exact: true }).selectOption("thorough");
  await editor.getByLabel("Maximum cost (USD)", { exact: true }).fill("5000");
  await expect(editor.getByText("Estimated cost:", { exact: false })).toContainText("USD 1000.0000");
  await expect(editor.getByLabel("Fidelity profile", { exact: true })).toHaveValue("production_safe");
  for (const name of ["Team", "Coordination", "Memory", "Verification", "Limits", "Recovery"]) await expect(editor.getByRole("group", { name, exact: true })).toBeVisible();
  for (const name of ["Cost and latency estimate", "Provider limits", "Required roles", "Seed support", "Every deployment cap", "Cap adjustments", "Warnings", "Effective differences from the fidelity profile", "Effective differences from the effort profile"]) {
    await expect(editor.getByRole("heading", { name, exact: true })).toBeVisible();
  }
  await expect(editor.getByText(/Ordinary endpoint edits affect new runs only/)).toBeVisible();
  await expect(editor.getByLabel("Specification input choices")).toBeHidden();
  await editor.getByText("Every immutable effective value", { exact: true }).click();
  await expect(editor.getByText("routing.endpoint sets", { exact: false }).first()).toBeVisible();
  await editor.getByRole("checkbox").check();
  await expect(editor.getByText(/Preview confirmed/)).toBeVisible();
  await expect(editor.getByRole("button", { name: "Submit native run · test only" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Send task", exact: true })).toBeDisabled();
  await editor.getByRole("heading", { name: "Classic specification", exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath("classic-editor.png") });
});

test("task and benchmark editors compile the same digest for equal inputs", async ({ page }) => {
  const inputs: string[] = [];
  page.on("request", (request) => { if (request.url().endsWith("/api/classic/spec") && request.method() === "POST") inputs.push(request.postData() ?? ""); });
  const editor = await openTaskEditor(page);
  await editor.getByLabel("Maximum cost (USD)", { exact: true }).fill("3.25");
  await expect(editor.getByText("Estimated cost:", { exact: false })).toContainText("USD 3.2500");
  const taskDigest = await page.getByTestId("classic-spec-digest").innerText();
  const taskInput = inputs.at(-1);
  await page.goto("/tests");
  await page.getByRole("button", { name: "New test", exact: true }).click();
  await page.getByRole("combobox", { name: "Runtime", exact: true }).selectOption("classic-native-preview");
  await expect(page.getByTestId("classic-spec-digest")).toBeVisible();
  await page.getByLabel("Maximum cost (USD)", { exact: true }).fill("3.25");
  await expect(page.getByTestId("classic-spec-digest")).toHaveText(taskDigest);
  expect(inputs.at(-1)).toBe(taskInput);
  await page.setViewportSize({ width: 375, height: 812 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await expect(page.getByRole("button", { name: "Run preflight", exact: true })).toBeDisabled();
  await expect(page.getByRole("button", { name: "Publish test", exact: true })).toBeDisabled();
});

test("keyboard navigation preserves focus order and visible focus through recompilation", async ({ page }) => {
  const editor = await openTaskEditor(page);
  const fidelity = editor.getByLabel("Fidelity profile", { exact: true });
  const effort = editor.getByLabel("Effort profile", { exact: true });
  await fidelity.focus();
  await page.keyboard.press("Tab");
  await expect(effort).toBeFocused();
  await page.keyboard.press("t");
  await page.keyboard.press("Tab");
  await expect(editor.getByLabel("Experts for complex tasks", { exact: true })).toBeFocused();
  await expect(editor.getByTestId("classic-spec-digest")).toBeVisible();
  await expect(editor.getByLabel("Experts for complex tasks", { exact: true })).toBeFocused();
  const outline = await editor.getByLabel("Experts for complex tasks", { exact: true }).evaluate((element) => getComputedStyle(element).outlineWidth);
  expect(parseFloat(outline)).toBeGreaterThanOrEqual(2);
  await page.keyboard.press("Tab");
  await expect(editor.getByLabel("Random seed", { exact: true })).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(editor.getByText("More team controls", { exact: true })).toBeFocused();
  await page.keyboard.press("Enter");
  await page.keyboard.press("Tab");
  await expect(editor.getByLabel("Team / experts by tier / simple", { exact: true })).toBeFocused();
});

test("mobile and reduced-motion preview has no horizontal overflow", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await page.emulateMedia({ reducedMotion: "reduce" });
  const editor = await openTaskEditor(page);
  await editor.getByText("Every immutable effective value", { exact: true }).click();
  expect(await editor.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  expect(await editor.evaluate((element) => Array.from(element.querySelectorAll("*")).every((item) => getComputedStyle(item).animationName === "none"))).toBe(true);
  await editor.getByRole("checkbox").check();
  await expect(editor.getByText(/Preview confirmed/)).toBeVisible();
});

test("errors stay beside controls and corrections invalidate prior confirmation", async ({ page }) => {
  const editor = await openTaskEditor(page);
  await editor.getByRole("checkbox").check();
  const cost = editor.getByLabel("Maximum cost (USD)", { exact: true });
  await cost.fill("invalid");
  await expect(cost).toHaveAttribute("aria-invalid", "true");
  const errorId = (await cost.getAttribute("aria-describedby"))?.split(" ").at(-1);
  await expect(page.locator(`[id="${errorId}"]`)).toContainText("Invalid money amount");
  await expect(editor.getByTestId("classic-spec-digest")).toHaveCount(0);
  await cost.fill("1.25");
  await expect(editor.getByTestId("classic-spec-digest")).toBeVisible();
  await expect(editor.getByRole("checkbox")).not.toBeChecked();
  await editor.getByText("Advanced JSON", { exact: true }).click();
  await editor.getByLabel("Specification input choices").fill("{");
  await expect(editor.getByLabel("Specification input choices")).toHaveAttribute("aria-invalid", "true");
  await expect(editor.getByTestId("classic-spec-digest")).toHaveCount(0);
});

test("a delayed obsolete preview cannot replace newer choices", async ({ page }) => {
  let release: (() => void) | undefined;
  let started: (() => void) | undefined;
  const blocked = new Promise<void>((resolve) => { started = resolve; });
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/classic/spec", async (route) => {
    const body = route.request().postData() ?? "{}";
    const response = await compileResponse(route.request().method(), body);
    if (body.includes('"7.25"')) { started?.(); await gate; }
    await route.fulfill({ status: response.status, json: response.body }).catch(() => undefined);
  });
  const editor = await openTaskEditor(page);
  const cost = editor.getByLabel("Maximum cost (USD)", { exact: true });
  await cost.fill("7.25");
  await blocked;
  await cost.fill("8.25");
  await expect(editor.getByText("Estimated cost:", { exact: false })).toContainText("USD 8.2500");
  const digest = await editor.getByTestId("classic-spec-digest").innerText();
  release?.();
  await expect(editor.getByTestId("classic-spec-digest")).toHaveText(digest);
  await expect(cost).toHaveValue("8.25");
});


test("late benchmark defaults preserve the selected native preview", async ({ page }) => {
  let release: (() => void) | undefined;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/benchmarks/scorers", async (route) => {
    await gate;
    await route.fulfill({ json: { scorers: [{ id: "exact", name: "Exact", version: "1", description: "Exact match" }] } });
  });
  await page.goto("/tests");
  await page.getByRole("button", { name: "New test", exact: true }).click();
  const runtime = page.getByRole("combobox", { name: "Runtime", exact: true });
  await runtime.selectOption("classic-native-preview");
  await expect(page.getByTestId("classic-spec-digest")).toBeVisible();
  const digest = await page.getByTestId("classic-spec-digest").innerText();
  release?.();
  await expect(page.getByRole("group", { name: "Scorers", exact: true }).getByRole("checkbox")).toBeChecked();
  await expect(runtime).toHaveValue("classic-native-preview");
  await expect(page.getByTestId("classic-spec-digest")).toHaveText(digest);
  await expect(page.getByRole("button", { name: "Run preflight", exact: true })).toBeDisabled();
});
