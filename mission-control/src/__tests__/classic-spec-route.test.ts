import { afterEach, describe, expect, it, vi } from "vitest";
vi.mock("@/lib/config", () => ({ DAEMON_BASE_URL: "http://daemon" }));
import { GET, POST } from "@/app/api/classic/spec/route";
import { effectiveRows, parseClassicPreview, parseClassicSchema } from "@/lib/classic-spec";

afterEach(() => { vi.unstubAllGlobals(); vi.unstubAllEnvs(); });

describe("Classic preview proxy", () => {
  it("publishes the daemon schema with authentication and no cache", async () => {
    vi.stubEnv("BMAS_API_KEY", "operator-key");
    const fetcher = vi.fn(async () => Response.json({ type: "object" }));
    vi.stubGlobal("fetch", fetcher);
    const response = await GET();
    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(fetcher).toHaveBeenCalledWith("http://daemon/classic/spec/schema", expect.objectContaining({ cache: "no-store", headers: expect.any(Headers) }));
    const init = (fetcher.mock.calls[0] as unknown as [string, RequestInit])[1];
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer operator-key");
  });
  it("forwards equal task and arm choices unchanged to the same compiler", async () => {
    const fetcher = vi.fn(async () => Response.json({ specification_digest: "same" }));
    vi.stubGlobal("fetch", fetcher);
    const input = { fidelity: "production_safe", effort: "standard", task_overrides: { classic: { "limits.max_cost": "1.25" }, seed: 42 } };
    for (const surface of ["task", "arm"]) await POST(new Request(`http://ui/api/classic/spec?surface=${surface}`, { method: "POST", body: JSON.stringify(input) }));
    const calls = fetcher.mock.calls as unknown as Array<[string, RequestInit]>;
    expect(calls.map(([url]) => url)).toEqual(["http://daemon/classic/spec/compile", "http://daemon/classic/spec/compile"]);
    expect(calls[0][1].body).toBe(calls[1][1].body);
    expect(JSON.parse(String(calls[0][1].body))).toEqual(input);
  });
  it("preserves field errors and the upstream rejection status", async () => {
    const errors = { detail: { errors: [{ field: "limits.max_cost", message: "Invalid money" }] } };
    vi.stubGlobal("fetch", vi.fn(async () => Response.json(errors, { status: 422 })));
    const response = await POST(new Request("http://ui/api/classic/spec", { method: "POST", body: "{}" }));
    expect(response.status).toBe(422);
    expect(await response.json()).toEqual(errors);
  });
  it("rejects malformed JSON before calling the daemon", async () => {
    const fetcher = vi.fn(); vi.stubGlobal("fetch", fetcher);
    const response = await POST(new Request("http://ui/api/classic/spec", { method: "POST", body: "{" }));
    expect(response.status).toBe(400);
    expect(fetcher).not.toHaveBeenCalled();
  });
  it("returns a safe retry message when the daemon fails", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("private endpoint details"); }));
    const response = await GET();
    expect(response.status).toBe(503);
    expect(JSON.stringify(await response.json())).not.toContain("private endpoint");
  });
  it("rejects incomplete contracts", () => {
    for (const value of [null, [], {}, { "x-editor": {} }, { specification_digest: "partial", specification: {} }]) {
      expect(() => parseClassicSchema(value)).toThrow();
      expect(() => parseClassicPreview(value)).toThrow();
    }
  });
  it("retains every leaf, empty collection, null and exact integer in the readable view", () => {
    expect(effectiveRows({ limits: { amount_nanos: 1000000000, empty: [], unset: null }, roles: ["planner", "critic"] })).toEqual([
      ["limits.amount_nanos", 1000000000], ["limits.empty", []], ["limits.unset", null], ["roles / 1", "planner"], ["roles / 2", "critic"],
    ]);
  });
});
