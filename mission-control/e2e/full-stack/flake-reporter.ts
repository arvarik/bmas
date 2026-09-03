import type { FullResult, Reporter, TestCase, TestResult } from "@playwright/test/reporter";

/**
 * Report every retried-then-passed test as flaky and fail the declared
 * flake budget. Playwright keeps the first failed attempt's artifacts
 * because the configuration preserves output for every attempt; this
 * reporter makes sure the first failure never hides behind a passing
 * retry.
 */
export default class FlakeReporter implements Reporter {
  private readonly budget = Number(process.env.BMAS_FLAKE_BUDGET ?? "0");
  private readonly flaky: string[] = [];

  onTestEnd(test: TestCase, result: TestResult): void {
    if (result.retry > 0 && result.status === "passed") {
      this.flaky.push(`${test.title} (passed on retry ${result.retry})`);
    }
  }

  async onEnd(result: FullResult): Promise<{ status?: FullResult["status"] } | undefined> {
    if (this.flaky.length === 0) return undefined;
    for (const entry of this.flaky) {
      console.log(`FLAKY: ${entry}; the first failed attempt's artifacts are kept`);
    }
    if (this.flaky.length > this.budget) {
      console.log(`FAIL: ${this.flaky.length} flaky tests exceed the flake budget of ${this.budget}`);
      return { status: "failed" };
    }
    return { status: result.status };
  }
}
