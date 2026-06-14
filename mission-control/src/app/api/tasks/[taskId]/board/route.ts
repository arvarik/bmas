import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * GET /api/tasks/[taskId]/board
 *
 * Proxies to the daemon's GET /tasks/{taskId}/board, which returns the
 * durable Redis board snapshot (no TTL) written by the board-persist hook.
 * This is available for live AND completed tasks, so the Blackboard view
 * never loses content on refetch/reload.
 *
 * Returns { entries: BoardEntry[], meta: {...} } with full envelope fields
 * (type, author, body, refs, confidence, salience, status, round, seq).
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;

  if (!taskId || !/^task-[\da-f]+$/i.test(taskId)) {
    return NextResponse.json({ error: "Invalid task ID" }, { status: 400 });
  }

  try {
    const upstream = await fetch(`${DAEMON_BASE_URL}/tasks/${taskId}/board`, {
      signal: AbortSignal.timeout(5_000),
      cache: "no-store",
    });

    if (upstream.status === 404) {
      return NextResponse.json({ entries: [], meta: {} });
    }

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
      { error: "Failed to fetch board entries", detail: message },
      { status: 503 },
    );
  }
}
