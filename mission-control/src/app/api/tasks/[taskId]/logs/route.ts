import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;
  const { searchParams } = new URL(request.url);

  const qp = new URLSearchParams();
  for (const key of ["limit", "offset"]) {
    const val = searchParams.get(key);
    if (val) qp.set(key, val);
  }

  const qs = qp.toString();
  const url = `${DAEMON_BASE_URL}/tasks/${taskId}/logs${qs ? `?${qs}` : ""}`;

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
