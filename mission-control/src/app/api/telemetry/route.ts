import { NextResponse } from "next/server";
import { BESZEL_HUB_URL } from "@/lib/config";

export async function GET(): Promise<NextResponse> {
  if (!BESZEL_HUB_URL) {
    return NextResponse.json({
      hub_status: "not_configured",
      note: "Monitoring is not configured in bmas.yaml. Add monitoring.beszel_hub to enable.",
    });
  }

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
