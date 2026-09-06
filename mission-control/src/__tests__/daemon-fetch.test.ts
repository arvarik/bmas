/**
 * Every daemon request the dashboard proxies carries the operator key
 * once one is configured, on reads as well as mutations, and keeps any
 * header the caller set.
 */
import { afterEach, describe, expect, it, vi } from "vitest";

import { daemonFetch, daemonHeaders } from "@/lib/daemon-fetch";

describe("daemon fetch credentials", () => {
  const original = process.env.BMAS_API_KEY;
  afterEach(() => {
    if (original === undefined) delete process.env.BMAS_API_KEY;
    else process.env.BMAS_API_KEY = original;
    vi.restoreAllMocks();
  });

  it("adds the bearer key and the operator identity when configured", () => {
    process.env.BMAS_API_KEY = "operator-secret";
    expect(daemonHeaders({ Accept: "text/event-stream" }, "alice")).toEqual({
      Accept: "text/event-stream",
      Authorization: "Bearer operator-secret",
      "X-Operator-Id": "alice",
    });
    delete process.env.BMAS_API_KEY;
    expect(daemonHeaders()).toEqual({});
  });

  it("sends the key on a plain read and keeps caller headers", async () => {
    process.env.BMAS_API_KEY = "operator-secret";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    await daemonFetch("http://daemon/tasks/task-a", { cache: "no-store", headers: { "X-Custom": "1" } });
    const [, init] = fetchMock.mock.calls[0];
    const headers = new Headers((init as RequestInit).headers);
    expect(headers.get("authorization")).toBe("Bearer operator-secret");
    expect(headers.get("x-custom")).toBe("1");
    expect((init as RequestInit).cache).toBe("no-store");
  });

  it("never overrides an explicit authorization header", async () => {
    process.env.BMAS_API_KEY = "operator-secret";
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response("{}", { status: 200 }));
    await daemonFetch("http://daemon/ingest", { headers: { Authorization: "Bearer node-secret" } });
    const headers = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    expect(headers.get("authorization")).toBe("Bearer node-secret");
  });
});
