import { createHash, timingSafeEqual } from "node:crypto";
import { NextRequest, NextResponse } from "next/server";

const DASHBOARD_KEY_HEADER = "x-bmas-dashboard-key";
const OPERATOR_ID_HEADER = "x-bmas-operator-id";

function keysMatch(actual: string, expected: string): boolean {
  const actualDigest = createHash("sha256").update(actual).digest();
  const expectedDigest = createHash("sha256").update(expected).digest();
  return timingSafeEqual(actualDigest, expectedDigest);
}

function basicCredentials(authorization: string): { username: string; password: string } | null {
  const match = authorization.match(/^Basic\s+(.+)$/i);
  if (!match) return null;

  try {
    const decoded = Buffer.from(match[1], "base64").toString("utf8");
    const separator = decoded.indexOf(":");
    return separator >= 0
      ? { username: decoded.slice(0, separator), password: decoded.slice(separator + 1) }
      : null;
  } catch {
    return null;
  }
}

function authenticatedOperator(
  request: NextRequest,
  expectedKey: string,
): string | null {
  const dashboardHeader = request.headers.get(DASHBOARD_KEY_HEADER);
  if (dashboardHeader && keysMatch(dashboardHeader, expectedKey)) return "dashboard-key";

  const authorization = request.headers.get("authorization") ?? "";
  const bearer = authorization.match(/^Bearer\s+(.+)$/i)?.[1];
  if (bearer && keysMatch(bearer, expectedKey)) return "bearer-operator";

  const credentials = basicCredentials(authorization);
  if (!credentials || !keysMatch(credentials.password, expectedKey)) return null;
  const username = credentials.username
    .trim()
    .replace(/[^a-zA-Z0-9_.@-]/g, "-")
    .slice(0, 128);
  return username || "basic-operator";
}

function authenticatedRequest(request: NextRequest, operatorId: string): NextResponse {
  const headers = new Headers(request.headers);
  headers.set(OPERATOR_ID_HEADER, operatorId);
  return NextResponse.next({ request: { headers } });
}

export function proxy(request: NextRequest): NextResponse {
  const dashboardKey = process.env.BMAS_DASHBOARD_KEY ?? "";
  if (!dashboardKey) return authenticatedRequest(request, "local-operator");
  const operatorId = authenticatedOperator(request, dashboardKey);
  if (operatorId) return authenticatedRequest(request, operatorId);

  return NextResponse.json(
    { error: "Mission Control authentication required" },
    {
      status: 401,
      headers: {
        "Cache-Control": "no-store",
        "WWW-Authenticate":
          'Basic realm="Stigmergic Mission Control", charset="UTF-8"',
      },
    },
  );
}

// The browser fetches the service worker script, the web manifest, the
// icons, and robots.txt without the dashboard header or basic
// credentials, so those public files stay outside authentication.
// Every page and every API route stays behind it.
//
// Next.js parses this matcher at compile time, so the entry must be one
// static string. Do not build it from a variable or a template literal.
// The public file names anchor at the end of the path, so "/sw.json" or
// "/tasks/sw.js" stay protected.
export const config = {
  matcher: [
    "/((?!api/health|_next/static|_next/image|(?:sw\\.js|manifest\\.webmanifest|robots\\.txt|favicon\\.ico|favicon-16x16\\.png|favicon-32x32\\.png|icon\\.png|apple-icon\\.png|apple-touch-icon\\.png|android-chrome-192x192\\.png|android-chrome-512x512\\.png|ant-head\\.png)$).*)",
  ],
};
