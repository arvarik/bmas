import { defineConfig, devices } from "@playwright/test";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

/**
 * Two projects share this configuration.
 *
 * The mocked component project keeps the existing browser tests: they
 * run against a Mission Control dev server with mocked daemon routes
 * and stay parallel. The full-stack project runs the unmocked journey
 * against the complete local stack the test-stack controller starts
 * in global setup: every service is real, the project runs with one
 * worker, every attempt's artifacts persist, and a retried-then-passed
 * test reports as flaky and fails the flake budget.
 */
const ENV_FILE = path.resolve(__dirname, "test-results/full-stack/test-env.json");
const fullStack = process.env.BMAS_E2E_PROJECT === "full-stack";

function fullStackBaseUrl(): string {
  if (!existsSync(ENV_FILE)) return "http://127.0.0.1:43000";
  const stack = JSON.parse(readFileSync(ENV_FILE, "utf-8")) as { urls: Record<string, string> };
  return stack.urls.mission_control;
}

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  preserveOutput: "always",
  reporter: fullStack
    ? [["list"], ["./e2e/full-stack/flake-reporter.ts"], ["html", { open: "never", outputFolder: "playwright-report/full-stack" }]]
    : process.env.CI ? "github" : "list",
  globalSetup: fullStack ? "./e2e/full-stack/global-setup.ts" : undefined,
  globalTeardown: fullStack ? "./e2e/full-stack/global-teardown.ts" : undefined,
  use: {
    baseURL: fullStack ? fullStackBaseUrl() : "http://127.0.0.1:3100",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: fullStack
    ? undefined
    : {
        command: "BMAS_CONFIG=../bmas.example.yaml REDIS_PASSWORD=e2e npm run dev -- --hostname 127.0.0.1 --port 3100",
        url: "http://127.0.0.1:3100",
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
  projects: fullStack
    ? [{
        name: "full-stack",
        testMatch: /full-stack\/.*\.spec\.ts/,
        use: { ...devices["Desktop Chrome"], baseURL: fullStackBaseUrl() },
      }]
    : [{
        name: "chromium",
        testIgnore: /full-stack\//,
        use: { ...devices["Desktop Chrome"] },
      }],
});
