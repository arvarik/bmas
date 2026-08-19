import { afterEach, describe, expect, it } from "vitest";
import { NextRequest } from "next/server";
import { config, proxy } from "@/proxy";

const originalDashboardKey = process.env.BMAS_DASHBOARD_KEY;

function request(headers: HeadersInit = {}): NextRequest {
  return new NextRequest("http://dashboard.test/api/submit", { headers });
}

afterEach(() => {
  if (originalDashboardKey === undefined) {
    delete process.env.BMAS_DASHBOARD_KEY;
  } else {
    process.env.BMAS_DASHBOARD_KEY = originalDashboardKey;
  }
});

describe("Mission Control request authentication", () => {
  it("leaves only health and Next.js asset paths public", () => {
    expect(config.matcher).toEqual([
      "/((?!api/health|_next/static|_next/image).*)",
    ]);
  });

  it("keeps trusted deployments compatible when no key is configured", () => {
    delete process.env.BMAS_DASHBOARD_KEY;

    const response = proxy(request());

    expect(response.status).toBe(200);
    expect(response.headers.get("x-middleware-next")).toBe("1");
    expect(response.headers.get("x-middleware-request-x-bmas-operator-id")).toBe("local-operator");
  });

  it("rejects a request without credentials in protected mode", async () => {
    process.env.BMAS_DASHBOARD_KEY = "dashboard-secret";

    const response = proxy(request());

    expect(response.status).toBe(401);
    expect(response.headers.get("cache-control")).toBe("no-store");
    expect(response.headers.get("www-authenticate")).toContain("Basic realm=");
    await expect(response.json()).resolves.toEqual({
      error: "Mission Control authentication required",
    });
  });

  it("accepts the dashboard key as a bearer credential", () => {
    process.env.BMAS_DASHBOARD_KEY = "dashboard-secret";

    const response = proxy(
      request({ Authorization: "Bearer dashboard-secret" }),
    );

    expect(response.status).toBe(200);
  });

  it("accepts the dashboard key through the dashboard header", () => {
    process.env.BMAS_DASHBOARD_KEY = "dashboard-secret";

    const response = proxy(
      request({ "X-BMAS-Dashboard-Key": "dashboard-secret" }),
    );

    expect(response.status).toBe(200);
  });

  it("accepts HTTP Basic authentication for browser access", () => {
    process.env.BMAS_DASHBOARD_KEY = "dashboard-secret";
    const credentials = Buffer.from("operator:dashboard-secret").toString(
      "base64",
    );

    const response = proxy(
      request({ Authorization: `Basic ${credentials}` }),
    );

    expect(response.status).toBe(200);
    expect(response.headers.get("x-middleware-request-x-bmas-operator-id")).toBe("operator");
  });

  it("rejects incorrect and malformed credentials", () => {
    process.env.BMAS_DASHBOARD_KEY = "dashboard-secret";

    expect(
      proxy(request({ Authorization: "Bearer wrong-secret" })).status,
    ).toBe(401);
    expect(proxy(request({ Authorization: "Basic not-base64" })).status).toBe(
      401,
    );
    expect(
      proxy(request({ "X-BMAS-Dashboard-Key": "wrong-secret" })).status,
    ).toBe(401);
  });
});
