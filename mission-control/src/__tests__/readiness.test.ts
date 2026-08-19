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
      provider_credentials: [{
        alias: "starter-model",
        provider: "gemini",
        env_var: "GEMINI_API_KEY",
        required: true,
        configured: false,
      }],
      agent_health: { agent: { alive: false } },
      storage: { enabled: true, ready: false },
      task_queue: { queued_tasks: 2, queue_capacity: 10 },
      litellm_connected: true,
      redis_connected: true,
    });

    expect(document.status).toBe("not_ready");
    expect(document.checks[0].fix).toContain("logs agent");
    expect(document.provider_credentials[0].env_var).toBe("GEMINI_API_KEY");
    expect(document.task_queue.queued_tasks).toBe(2);
  });

  it("rejects an incomplete readiness check", () => {
    expect(() => parseReadiness({
      status: "ready",
      checks: [{ id: "agent", ready: true }],
    })).toThrow("invalid readiness check");
  });
});
