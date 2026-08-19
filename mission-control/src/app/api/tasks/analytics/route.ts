import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

export async function GET(): Promise<NextResponse> {
  try {
    const upstream = await fetch(`${DAEMON_BASE_URL}/tasks/analytics`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
    return NextResponse.json(await upstream.json(), { status: upstream.status });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Task analytics are unavailable",
        detail: error instanceof Error ? error.message : "Unknown upstream error",
      },
      { status: 503 },
    );
  }
}
