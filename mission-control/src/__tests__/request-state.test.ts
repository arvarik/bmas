import { describe, expect, it, vi } from "vitest";
import {
  diagnosticsText,
  failureFromReason,
  failureFromResponse,
} from "@/lib/request-state";

describe("request state", () => {
  it("classifies denied requests as permission failures", async () => {
    const failure = await failureFromResponse(new Response(JSON.stringify({
      error: "Access denied",
      detail: "The agent rejected this key.",
    }), {
      status: 403,
      headers: { "Content-Type": "application/json" },
    }), "Request failed");

    expect(failure).toEqual({
      kind: "permission",
      message: "Access denied",
      detail: "The agent rejected this key.",
      status: 403,
    });
  });

  it("classifies dependency failures as unavailable", async () => {
    const failure = await failureFromResponse(new Response("", { status: 503 }), "Beszel request failed");

    expect(failure.kind).toBe("unavailable");
    expect(failure.detail).toContain("HTTP 503");
  });

  it("classifies network errors as unavailable", () => {
    expect(failureFromReason(new TypeError("Failed to fetch"), "Request failed").kind)
      .toBe("unavailable");
  });

  it("creates diagnostics with the resource context", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-08-19T12:00:00.000Z"));

    const text = diagnosticsText("Hermes sessions", {
      kind: "permission",
      message: "Access denied",
      detail: "The key cannot read sessions.",
      status: 403,
    }, { node: "planner" });

    expect(JSON.parse(text)).toMatchObject({
      component: "Hermes sessions",
      state: "permission",
      status: 403,
      node: "planner",
      captured_at: "2026-08-19T12:00:00.000Z",
    });

    vi.useRealTimers();
  });
});
