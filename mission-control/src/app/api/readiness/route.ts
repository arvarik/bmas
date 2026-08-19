import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/** Return the daemon's actionable readiness checks. */
export async function GET(): Promise<NextResponse> {
  try {
    const upstream = await fetch(`${DAEMON_BASE_URL}/readiness`, {
      signal: AbortSignal.timeout(3_000),
      cache: "no-store",
    });
    const body: unknown = await upstream.json().catch(() => ({
      error: `Daemon returned ${upstream.status}`,
    }));
    return NextResponse.json(body, {
      status: upstream.status,
      headers: { "Cache-Control": "no-store" },
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "Unknown upstream error";
    return NextResponse.json(
      {
        status: "not_ready",
        error: "Daemon readiness unavailable",
        detail,
        checks: [],
      },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
