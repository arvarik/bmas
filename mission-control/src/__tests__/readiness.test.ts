import { describe, expect, it } from "vitest";
import { parseReadiness } from "@/components/features/ReadinessPanel";

describe("readiness contract", () => {
  it("accepts an actionable readiness document", () => {
    const document = parseReadiness({
      status: "not_ready",
      checks: [{
        id: "agent",
        label: "Execution agent",
        ready: false,
        detail: "The agent is not reachable.",
        fix: "Run: docker compose logs agent",
      }],
    });

    expect(document.status).toBe("not_ready");
    expect(document.checks[0].fix).toContain("logs agent");
  });

  it("rejects an incomplete readiness check", () => {
    expect(() => parseReadiness({
      status: "ready",
      checks: [{ id: "agent", ready: true }],
    })).toThrow("invalid readiness check");
  });
});
