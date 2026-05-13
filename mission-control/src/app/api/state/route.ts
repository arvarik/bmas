import { NextResponse } from "next/server";

const DAEMON_STATE_URL =
  process.env.DAEMON_STATE_URL ?? "http://192.168.4.240:9000/state";

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
