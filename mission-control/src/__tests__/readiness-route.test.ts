import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({ DAEMON_BASE_URL: "http://daemon" }));

import { GET } from "@/app/api/readiness/route";

describe("readiness proxy", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("returns the daemon readiness document without caching", async () => {
    const upstreamFetch = vi.fn(async () => Response.json({
      status: "ready",
      checks: [],
    }));
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(upstreamFetch).toHaveBeenCalledWith(
      "http://daemon/readiness",
      expect.objectContaining({ cache: "no-store" }),
    );
  });

  it("returns an actionable error when the daemon is unavailable", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => {
      throw new Error("connection refused");
    }));

    const response = await GET();
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(body).toMatchObject({
      status: "not_ready",
      error: "Daemon readiness unavailable",
    });
  });
});
