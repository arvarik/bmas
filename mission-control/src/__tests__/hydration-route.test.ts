import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({ DAEMON_BASE_URL: "http://daemon" }));

import { GET } from "@/app/api/tasks/[taskId]/hydrate/route";

describe("task hydration route", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("accepts a valid legacy or custom task ID", async () => {
    const upstreamFetch = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith("/board")) return Response.json({ entries: [] });
      if (url.endsWith("/turns")) return Response.json({ turns: [] });
      if (url.endsWith("/cost")) return Response.json({ total_cost: 0 });
      if (url.includes("/logs?")) return Response.json({ entries: [] });
      if (url.includes("/trace?")) return Response.json({ traces: [] });
      return Response.json({ task: { id: "legacy_task-7", variant: "traditional" } });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(new Request("http://mission-control"), {
      params: Promise.resolve({ taskId: "legacy_task-7" }),
    });
    expect(response.status).toBe(200);
    expect(upstreamFetch).toHaveBeenCalledTimes(6);
    expect(upstreamFetch.mock.calls.map(([url]) => String(url))).toContainEqual(
      expect.stringContaining("/tasks/legacy_task-7"),
    );
  });

  it("rejects a traversal task ID before any daemon request", async () => {
    const upstreamFetch = vi.fn();
    vi.stubGlobal("fetch", upstreamFetch);
    const response = await GET(new Request("http://mission-control"), {
      params: Promise.resolve({ taskId: "../secret" }),
    });
    expect(response.status).toBe(400);
    expect(upstreamFetch).not.toHaveBeenCalled();
  });

  it("retries a failed tail page and returns the newest records", async () => {
    let failedOnce = false;
    const firstPage = Array.from({ length: 1_000 }, (_, index) => ({ id: index }));
    const upstreamFetch = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith("/board")) return Response.json({ entries: [] });
      if (url.endsWith("/turns")) return Response.json({ turns: [] });
      if (url.endsWith("/cost")) return Response.json({ total_cost: 0 });
      if (url.includes("/trace?")) return Response.json({ traces: [] });
      if (url.includes("/logs?") && url.includes("offset=0")) {
        return Response.json({ entries: firstPage });
      }
      if (url.includes("/logs?") && url.includes("offset=1000")) {
        if (!failedOnce) {
          failedOnce = true;
          return Response.json({ error: "temporary" }, { status: 503 });
        }
        return Response.json({ entries: [{ id: "newest" }] });
      }
      return Response.json({ task: { id: "task-1", variant: "classic" } });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(new Request("http://mission-control"), {
      params: Promise.resolve({ taskId: "task-1" }),
    });
    const body = await response.json();

    expect(response.status).toBe(200);
    expect(body.logs).toEqual({ entries: [{ id: "newest" }] });
    expect(upstreamFetch.mock.calls.filter(([url]) => (
      String(url).includes("/logs?") && String(url).includes("offset=1000")
    ))).toHaveLength(2);
  });

  it("exposes a persistent tail source failure", async () => {
    const firstPage = Array.from({ length: 1_000 }, (_, index) => ({ id: index }));
    const upstreamFetch = vi.fn(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith("/board")) return Response.json({ entries: [] });
      if (url.endsWith("/turns")) return Response.json({ turns: [] });
      if (url.endsWith("/cost")) return Response.json({ total_cost: 0 });
      if (url.includes("/trace?")) return Response.json({ traces: [] });
      if (url.includes("/logs?") && url.includes("offset=0")) {
        return Response.json({ entries: firstPage });
      }
      if (url.includes("/logs?")) {
        return Response.json({ error: "unavailable" }, { status: 503 });
      }
      return Response.json({ task: { id: "task-1", variant: "classic" } });
    });
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await GET(new Request("http://mission-control"), {
      params: Promise.resolve({ taskId: "task-1" }),
    });
    const body = await response.json();

    expect(response.status).toBe(503);
    expect(body).toMatchObject({ error: "Daemon unreachable" });
    expect(body.detail).toContain("logs hydration returned HTTP 503");
  });
});
