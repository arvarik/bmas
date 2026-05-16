import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

export async function GET(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  const params = new URLSearchParams();

  // Forward supported query params
  for (const key of ["limit", "offset", "status"]) {
    const val = searchParams.get(key);
    if (val) params.set(key, val);
  }

  const qs = params.toString();
  const url = `${DAEMON_BASE_URL}/tasks${qs ? `?${qs}` : ""}`;

  try {
    const upstream = await fetch(url, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });

    if (!upstream.ok) {
      return NextResponse.json(
        { error: `Daemon returned ${upstream.status}` },
        { status: upstream.status },
      );
    }

    return NextResponse.json(await upstream.json());
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: "Daemon unreachable", detail: message },
      { status: 503 },
    );
  }
}
