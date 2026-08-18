import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({ DAEMON_BASE_URL: "http://daemon" }));

import { GET } from "@/app/api/stream/system/route";

describe("system stream proxy", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("forwards the browser replay cursor to the daemon", async () => {
    const upstreamFetch = vi.fn<(
      input: string | URL | Request,
      init?: RequestInit,
    ) => Promise<Response>>(async () => new Response(
      "event: task-completed\ndata: {}\n\n",
      { status: 200, headers: { "Content-Type": "text/event-stream" } },
    ));
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(new Request(
      "http://mission-control/api/stream/system",
      { headers: { "Last-Event-ID": "42" } },
    ));

    expect(response.status).toBe(200);
    expect(upstreamFetch).toHaveBeenCalledOnce();
    expect(upstreamFetch.mock.calls[0][1]?.headers).toEqual({
      "Last-Event-ID": "42",
    });
  });

  it("retries a replay cursor gap once without the cursor", async () => {
    let requestCount = 0;
    const upstreamFetch = vi.fn<(
      input: string | URL | Request,
      init?: RequestInit,
    ) => Promise<Response>>(async () => {
      requestCount += 1;
      return requestCount === 1
        ? Response.json({ error: "event_cursor_gap" }, { status: 409 })
        : new Response("event: daemon-status\ndata: {}\n\n", {
            status: 200,
            headers: { "Content-Type": "text/event-stream" },
          });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(new Request(
      "http://mission-control/api/stream/system",
      { headers: { "Last-Event-ID": "99" } },
    ));

    expect(response.status).toBe(200);
    expect(upstreamFetch).toHaveBeenCalledTimes(2);
    expect(upstreamFetch.mock.calls[0][1]?.headers).toEqual({
      "Last-Event-ID": "99",
    });
    expect(upstreamFetch.mock.calls[1][1]?.headers).toBeUndefined();
  });

  it("does not retry an unrelated conflict", async () => {
    const upstreamFetch = vi.fn<(
      input: string | URL | Request,
      init?: RequestInit,
    ) => Promise<Response>>(async () => Response.json(
      { error: "system_conflict" },
      { status: 409 },
    ));
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(new Request(
      "http://mission-control/api/stream/system",
      { headers: { "Last-Event-ID": "7" } },
    ));

    expect(response.status).toBe(409);
    expect(upstreamFetch).toHaveBeenCalledOnce();
  });
});
