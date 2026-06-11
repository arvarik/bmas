import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * GET /api/tasks/[taskId]/artifacts — list artifacts for a task
 *
 * Proxies to daemon GET /tasks/{taskId}/artifacts
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;

  try {
    const upstream = await fetch(
      `${DAEMON_BASE_URL}/tasks/${taskId}/artifacts`,
      {
        cache: "no-store",
        signal: AbortSignal.timeout(5_000),
      },
    );

    if (upstream.status === 404) {
      // Task has no artifacts — return empty collection, not an error
      return NextResponse.json({ artifacts: [] });
    }

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
