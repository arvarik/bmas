import { describe, expect, it } from "vitest";
import { GET } from "@/app/api/health/route";

describe("Mission Control health route", () => {
  it("returns a fixed public health result", async () => {
    const response = GET();

    expect(response.status).toBe(200);
    expect(response.headers.get("cache-control")).toBe("no-store");
    await expect(response.json()).resolves.toEqual({ status: "ok" });
  });
});
