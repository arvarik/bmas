import { createHash, timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

const DASHBOARD_KEY_HEADER = "x-bmas-dashboard-key";

function keysMatch(actual: string, expected: string): boolean {
  const actualDigest = createHash("sha256").update(actual).digest();
  const expectedDigest = createHash("sha256").update(expected).digest();
  return timingSafeEqual(actualDigest, expectedDigest);
}

function basicPassword(authorization: string): string | null {
  const match = authorization.match(/^Basic\s+(.+)$/i);
  if (!match) return null;

  try {
    const decoded = Buffer.from(match[1], "base64").toString("utf8");
    const separator = decoded.indexOf(":");
    return separator >= 0 ? decoded.slice(separator + 1) : null;
  } catch {
    return null;
  }
}

function requestHasDashboardKey(
  request: NextRequest,
  expectedKey: string,
): boolean {
  const dashboardHeader = request.headers.get(DASHBOARD_KEY_HEADER);
  if (dashboardHeader && keysMatch(dashboardHeader, expectedKey)) return true;

  const authorization = request.headers.get("authorization") ?? "";
  const bearer = authorization.match(/^Bearer\s+(.+)$/i)?.[1];
  if (bearer && keysMatch(bearer, expectedKey)) return true;

  const password = basicPassword(authorization);
  return password !== null && keysMatch(password, expectedKey);
}

export function proxy(request: NextRequest): NextResponse {
  const dashboardKey = process.env.BMAS_DASHBOARD_KEY ?? "";
  if (!dashboardKey || requestHasDashboardKey(request, dashboardKey)) {
    return NextResponse.next();
  }

  return NextResponse.json(
    { error: "Mission Control authentication required" },
    {
      status: 401,
      headers: {
        "Cache-Control": "no-store",
        "WWW-Authenticate":
          'Basic realm="bMAS Mission Control", charset="UTF-8"',
      },
    },
  );
}

export const config = {
  matcher: ["/((?!_next/static|_next/image).*)"],
};
