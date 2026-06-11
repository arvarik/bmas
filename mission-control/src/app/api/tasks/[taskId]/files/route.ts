import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * GET /api/tasks/[taskId]/files — list files for a task
 *
 * Proxies to daemon GET /tasks/{taskId}/files
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;

  try {
    const upstream = await fetch(`${DAEMON_BASE_URL}/tasks/${taskId}/files`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });

    if (upstream.status === 404) {
      // Task has no files — return empty collection, not an error
      return NextResponse.json({ files: [] });
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

/**
 * POST /api/tasks/[taskId]/files — upload file
 *
 * Proxies multipart body to daemon POST /tasks/{taskId}/files
 */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;

  try {
    // Forward the multipart body as-is to the daemon
    const body = await request.arrayBuffer();
    const contentType = request.headers.get("content-type") || "";

    const upstream = await fetch(`${DAEMON_BASE_URL}/tasks/${taskId}/files`, {
      method: "POST",
      headers: {
        "content-type": contentType,
      },
      body: body,
      signal: AbortSignal.timeout(30_000), // uploads can be larger
    });

    if (!upstream.ok) {
      const errBody = await upstream.json().catch(() => ({}));
      return NextResponse.json(
        errBody as Record<string, unknown>,
        { status: upstream.status },
      );
    }

    return NextResponse.json(await upstream.json());
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: "Upload failed", detail: message },
      { status: 503 },
    );
  }
}
