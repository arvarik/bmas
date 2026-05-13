import { NextResponse } from "next/server";

/**
 * Beszel Hub is a PocketBase-based monitoring system.
 * The Hub runs on TrueNAS at 192.168.4.229:8090.
 *
 * Beszel requires authentication for most data endpoints.
 * This proxy route provides a server-side fetch so the browser
 * never hits CORS issues. If the Hub is unreachable or returns
 * an auth error, we return a graceful error to the frontend.
 */

const BESZEL_HUB_URL =
  process.env.BESZEL_HUB_URL ?? "http://192.168.4.229:8090";

export async function GET(): Promise<NextResponse> {
  try {
    // Try the Beszel health endpoint first
    const healthRes = await fetch(`${BESZEL_HUB_URL}/api/health`, {
      cache: "no-store",
      signal: AbortSignal.timeout(4_000),
    });

    if (!healthRes.ok) {
      return NextResponse.json(
        { error: `Beszel Hub returned ${healthRes.status}` },
        { status: healthRes.status },
      );
    }

    // Beszel uses PocketBase — system stats are in collections.
    // Without auth, we can at least confirm the Hub is alive and
    // return basic health status. Full telemetry requires auth tokens.
    //
    // For now, return a "hub alive" response with placeholder metrics.
    // TODO: Add PocketBase auth flow (email/password → token → /api/collections/systems/records)

    return NextResponse.json({
      hub_status: "connected",
      hub_url: BESZEL_HUB_URL,
      cpu: 0,
      memPct: 0,
      memUsed: 0,
      memTotal: 0,
      temperatures: [],
      note: "Beszel Hub requires authentication for detailed metrics. Hub is reachable.",
    });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json(
      { error: "Beszel Hub unreachable", detail: message },
      { status: 503 },
    );
  }
}
