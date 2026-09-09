import { expect, test } from "@playwright/test";
import { readStack } from "./stack";

const stack = readStack();
test.use({ viewport: { width: 1280, height: 2400 } });

test("Classic task and benchmark previews share the deployed compiler without native admission", async ({ page, request }) => {
  const schema = await request.get(`${stack.urls.mission_control}/api/classic/spec`);
  expect(schema.status()).toBe(200);
  expect((await schema.json())["x-editor"].availability).toBe("test_only");
  await page.goto("/");
  await page.getByRole("combobox", { name: "Runtime", exact: true }).click();
  await page.getByRole("option", { name: "Classic native · test only" }).click();
  await expect(page.getByTestId("classic-spec-digest")).toBeVisible();
  const taskDigest = await page.getByTestId("classic-spec-digest").innerText();
  await expect(page.getByRole("button", { name: "Send task", exact: true })).toBeDisabled();
  await page.goto("/tests");
  await page.getByRole("button", { name: "New test", exact: true }).click();
  await page.getByRole("combobox", { name: "Runtime", exact: true }).selectOption("classic-native-preview");
  await expect(page.getByTestId("classic-spec-digest")).toHaveText(taskDigest);
  await expect(page.getByRole("button", { name: "Run preflight", exact: true })).toBeDisabled();
});
