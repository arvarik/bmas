import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/config", () => ({
  AGENT_HOSTS: { starter: "http://agent:8000" },
  NODES: [{ role: "starter", name: "Starter", host: "agent", port: 8000 }],
}));

import { GET as getProfiles } from "@/app/api/profiles/route";
import { GET as getSkills } from "@/app/api/skills/route";
import { GET as getToolsets } from "@/app/api/toolsets/route";

const originalExecuteKey = process.env.BMAS_EXECUTE_KEY;

describe("Hermes agent API routes", () => {
  beforeEach(() => {
    process.env.BMAS_EXECUTE_KEY = "execute-secret";
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    if (originalExecuteKey === undefined) delete process.env.BMAS_EXECUTE_KEY;
    else process.env.BMAS_EXECUTE_KEY = originalExecuteKey;
  });

  it("reads skills through the authenticated agent bridge", async () => {
    const upstreamFetch = vi.fn<typeof fetch>(async () => Response.json({
      object: "list",
      data: [{ name: "web", category: "research" }],
    }));
    vi.stubGlobal("fetch", upstreamFetch);

    const response = await getSkills(new Request("http://ui/api/skills?node=starter"));

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({
      skills: [{ name: "web" }],
      read_only: true,
    });
    const [url, init] = upstreamFetch.mock.calls[0];
    expect(url).toBe("http://agent:8000/v1/skills");
    expect((init?.headers as Headers).get("authorization")).toBe("Bearer execute-secret");
  });

  it("returns resolved toolsets without a Dashboard request", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async () => Response.json({
      object: "list",
      platform: "api_server",
      data: [{ name: "terminal", enabled: true, configured: true, tools: ["run"] }],
    })));

    const response = await getToolsets(new Request("http://ui/api/toolsets?node=starter"));

    await expect(response.json()).resolves.toMatchObject({
      platform: "api_server",
      toolsets: [{ name: "terminal", enabled: true }],
      read_only: true,
    });
  });

  it("derives one active profile from the capability document", async () => {
    vi.stubGlobal("fetch", vi.fn<typeof fetch>(async (input) => {
      if (String(input).endsWith("/health/detailed")) {
        return Response.json({
          status: "healthy",
          ready: true,
          model: "planner",
          runs_api_ready: true,
          hermes_status: "ready",
          hermes_version: "0.20.4",
          execution_backend: "hermes-runs-api",
          current_task: "task-1",
        });
      }
      return Response.json({
        object: "hermes.api_server.capabilities",
        model: "planner",
      });
    }));

    const response = await getProfiles();

    await expect(response.json()).resolves.toMatchObject({
      nodes: [{
        role: "starter",
        reachable: true,
        profiles: [{ name: "planner", gateway_running: true }],
        health: {
          status: "ready",
          capacity: "busy",
          current_task: "task-1",
          current_task_reported: true,
          hermes_version: "0.20.4",
        },
      }],
    });
  });
});
