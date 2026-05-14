import { NextResponse } from "next/server";
import { DAEMON_STATE_URL } from "@/lib/config";

export async function GET(): Promise<NextResponse> {
  try {
    const upstream = await fetch(DAEMON_STATE_URL, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });

    if (!upstream.ok) {
      return NextResponse.json(
        { error: `Daemon returned ${upstream.status}` },
        { status: upstream.status },
      );
    }

    const data: unknown = await upstream.json();
    return NextResponse.json(data);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: "Daemon unreachable", detail: message },
      { status: 503 },
    );
  }
}
