import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";

/** Return only the capability document that the daemon reports. */
export async function GET(): Promise<NextResponse> {
  try {
    const upstream = await daemonFetch(`${DAEMON_BASE_URL}/capabilities`, {
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
      { error: "Daemon capabilities unavailable", detail },
      { status: 503, headers: { "Cache-Control": "no-store" } },
    );
  }
}
