import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * GET /api/tasks/[taskId]/trace
 *
 * Proxies to daemon's GET /tasks/{taskId}/trace to fetch archived
 * trace events for completed tasks.
 */
export async function GET(
  request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;

  if (!taskId || !/^task-[\da-f]+$/i.test(taskId)) {
    return NextResponse.json({ error: "Invalid task ID" }, { status: 400 });
  }

  const url = new URL(request.url);
  const limit = url.searchParams.get("limit") ?? "200";
  const offset = url.searchParams.get("offset") ?? "0";

  try {
    const upstream = await fetch(
      `${DAEMON_BASE_URL}/tasks/${taskId}/trace?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`,
      {
        signal: AbortSignal.timeout(5_000),
        cache: "no-store",
      },
    );

    if (!upstream.ok) {
      const detail = await upstream.text().catch(() => "");
      return NextResponse.json(
        { error: `Daemon returned ${upstream.status}`, detail },
        { status: upstream.status },
      );
    }

    const data: unknown = await upstream.json();
    return NextResponse.json(data);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json(
      { error: "Failed to fetch traces", detail: message },
      { status: 503 },
    );
  }
}
