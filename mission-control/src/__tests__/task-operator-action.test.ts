import { afterEach, describe, expect, it, vi } from "vitest";

import { requestTaskOperatorAction } from "@/hooks/useTaskOperatorAction";

describe("task operator actions", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("returns the accepted response and sends a timeout signal", async () => {
    const requestFetch = vi.fn<typeof fetch>(async () => Response.json({ status: "pause_requested" }));
    vi.stubGlobal("fetch", requestFetch);

    const result = await requestTaskOperatorAction({
      action: "pause",
      task_id: "task-1",
    });

    expect(result).toEqual({ status: "pause_requested" });
    expect(requestFetch).toHaveBeenCalledWith("/api/hitl", expect.objectContaining({
      method: "POST",
      signal: expect.any(AbortSignal),
    }));
  });

  it("sends the stable idempotency key for safe retries", async () => {
    const requestFetch = vi.fn<typeof fetch>(async () => Response.json({ status: "pause_requested" }));
    vi.stubGlobal("fetch", requestFetch);

    await requestTaskOperatorAction({
      action: "pause",
      task_id: "task-1",
      idempotency_key: "action-one",
    });

    expect(requestFetch).toHaveBeenCalledWith("/api/hitl", expect.objectContaining({
      headers: expect.objectContaining({ "X-Idempotency-Key": "action-one" }),
    }));
  });

  it("uses the daemon detail for a failed action", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => Response.json(
      { error: "Task control request failed", detail: "The task queue is full." },
      { status: 429 },
    )));

    await expect(requestTaskOperatorAction({
      action: "resume",
      task_id: "task-1",
    })).rejects.toThrow("The task queue is full.");
  });

  it("returns a clear timeout message", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => {
      throw new DOMException("The request timed out.", "TimeoutError");
    }));

    await expect(requestTaskOperatorAction({
      action: "abort",
      task_id: "task-1",
    }, 8_000)).rejects.toThrow(
      "The request timed out after 8 seconds. Check the task state before you retry.",
    );
  });
});
