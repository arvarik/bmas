import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({ DAEMON_BASE_URL: "http://daemon" }));
vi.mock("@/lib/redis", () => ({ getRedis: vi.fn() }));

import { POST } from "@/app/api/hitl/route";

describe("Hermes Mission Control actions", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("forwards the four-value approval contract", async () => {
    const upstreamFetch = vi.fn<typeof fetch>(async () => Response.json({ resolved: 1 }));
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await POST(new Request("http://ui/api/hitl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "approval",
        task_id: "task-1",
        run_id: "run-1",
        choice: "session",
      }),
    }));

    expect(response.status).toBe(200);
    expect(upstreamFetch.mock.calls[0][0]).toBe("http://daemon/api/tasks/task-1/approval");
    expect(JSON.parse(String(upstreamFetch.mock.calls[0]?.[1]?.body))).toEqual({
      run_id: "run-1",
      choice: "session",
      reason: "",
    });
  });

  it("forwards live run guidance without using the board steer route", async () => {
    const upstreamFetch = vi.fn<typeof fetch>(async () => Response.json({ accepted: true }));
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await POST(new Request("http://ui/api/hitl", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "run-steer",
        task_id: "task-1",
        run_id: "run-1",
        input: "Focus on the failing test.",
      }),
    }));

    expect(response.status).toBe(200);
    expect(upstreamFetch.mock.calls[0][0]).toBe("http://daemon/api/tasks/task-1/run-steer");
    expect(JSON.parse(String(upstreamFetch.mock.calls[0]?.[1]?.body))).toEqual({
      run_id: "run-1",
      input: "Focus on the failing test.",
    });
  });

  it("forwards the operator idempotency key", async () => {
    const upstreamFetch = vi.fn<typeof fetch>(async () => Response.json({ status: "pause_requested" }));
    vi.stubGlobal("fetch", upstreamFetch);

    await POST(new Request("http://ui/api/hitl", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Idempotency-Key": "action-one",
        "X-BMAS-Operator-Id": "operator-a",
      },
      body: JSON.stringify({ action: "pause", task_id: "task-1" }),
    }));

    expect(upstreamFetch.mock.calls[0]?.[1]?.headers).toEqual(expect.objectContaining({
      "X-Idempotency-Key": "action-one",
      "X-Operator-Id": "operator-a",
    }));
  });
});
