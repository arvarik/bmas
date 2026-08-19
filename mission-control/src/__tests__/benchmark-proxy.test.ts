import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({ DAEMON_BASE_URL: "http://daemon" }));

import { benchmarkProxy } from "@/lib/benchmark-proxy";

describe("benchmark proxy", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("forwards only the authenticated operator identity", async () => {
    const upstreamFetch = vi.fn<typeof fetch>(async () => Response.json({ created: true }));
    vi.stubGlobal("fetch", upstreamFetch);

    await benchmarkProxy("/benchmarks/attempts/attempt-one/reviews", {
      method: "POST",
      request: new Request("http://ui/api/benchmarks/review", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-Idempotency-Key": "review-one",
          "X-Operator-Id": "spoofed",
          "X-BMAS-Operator-Id": "authenticated-operator",
        },
        body: JSON.stringify({ score: 1, passed: true, note: "Verified" }),
      }),
    });

    expect(upstreamFetch.mock.calls[0]?.[1]?.headers).toEqual(expect.objectContaining({
      "X-Idempotency-Key": "review-one",
      "X-Operator-Id": "authenticated-operator",
    }));
  });
});
