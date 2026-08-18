import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({ DAEMON_BASE_URL: "http://daemon" }));

import { GET } from "@/app/api/stream/task/[taskId]/route";

describe("task stream proxy", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("forwards the browser replay cursor to the daemon", async () => {
    const upstreamFetch = vi.fn<(
      input: string | URL | Request,
      init?: RequestInit,
    ) => Promise<Response>>(async () => new Response("event: phase\ndata: {}\n\n", {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    }));
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(
      new Request("http://mission-control/api/stream/task/task-abc", {
        headers: { "Last-Event-ID": "42" },
      }),
      { params: Promise.resolve({ taskId: "task-abc" }) },
    );

    expect(response.status).toBe(200);
    expect(upstreamFetch).toHaveBeenCalledOnce();
    expect(upstreamFetch.mock.calls[0][1]?.headers).toEqual({ "Last-Event-ID": "42" });
  });

  it("retries a replay cursor gap once without the cursor", async () => {
    let requestCount = 0;
    const upstreamFetch = vi.fn<(
      input: string | URL | Request,
      init?: RequestInit,
    ) => Promise<Response>>(async () => {
      requestCount += 1;
      return requestCount === 1
        ? Response.json(
            { error: "event_cursor_gap", recovery: "hydrate" },
            { status: 409 },
          )
        : new Response("event: initial_state\ndata: {}\n\n", {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(
      new Request("http://mission-control/api/stream/task/legacy_task-7", {
        headers: { "Last-Event-ID": "99" },
      }),
      { params: Promise.resolve({ taskId: "legacy_task-7" }) },
    );

    expect(response.status).toBe(200);
    expect(upstreamFetch).toHaveBeenCalledTimes(2);
    expect(upstreamFetch.mock.calls[0][1]?.headers).toEqual({ "Last-Event-ID": "99" });
    expect(upstreamFetch.mock.calls[1][1]?.headers).toBeUndefined();
  });

  it("does not retry an unrelated 409 response", async () => {
    const upstreamFetch = vi.fn<(
      input: string | URL | Request,
      init?: RequestInit,
    ) => Promise<Response>>(async () => Response.json(
      { error: "task_conflict" },
      { status: 409 },
    ));
    vi.stubGlobal("fetch", upstreamFetch);
    const response = await GET(
      new Request("http://mission-control", { headers: { "Last-Event-ID": "9" } }),
      { params: Promise.resolve({ taskId: "task-abc" }) },
    );
    expect(response.status).toBe(409);
    expect(upstreamFetch).toHaveBeenCalledOnce();
  });

  it("rejects a traversal task ID before the daemon request", async () => {
    const upstreamFetch = vi.fn();
    vi.stubGlobal("fetch", upstreamFetch);
    const response = await GET(
      new Request("http://mission-control"),
      { params: Promise.resolve({ taskId: "../secret" }) },
    );
    expect(response.status).toBe(400);
    expect(upstreamFetch).not.toHaveBeenCalled();
  });
});
