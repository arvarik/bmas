import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * GET /api/tasks/[taskId]/files/[fileId] — download a task file
 *
 * Proxies to daemon GET /tasks/{taskId}/files/{fileId} and streams
 * the binary content back with download-safe headers.
 */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string; fileId: string }> },
): Promise<Response> {
  const { taskId, fileId } = await params;

  try {
    const upstream = await fetch(
      `${DAEMON_BASE_URL}/tasks/${taskId}/files/${fileId}`,
      {
        cache: "no-store",
        signal: AbortSignal.timeout(15_000),
      },
    );

    if (!upstream.ok) {
      return NextResponse.json(
        { error: `Daemon returned ${upstream.status}` },
        { status: upstream.status },
      );
    }

    // Stream the response body through
    const headers = new Headers();
    const contentType = upstream.headers.get("content-type");
    const contentDisposition = upstream.headers.get("content-disposition");
    const contentLength = upstream.headers.get("content-length");

    if (contentType) headers.set("content-type", contentType);
    if (contentDisposition) headers.set("content-disposition", contentDisposition);
    if (contentLength) headers.set("content-length", contentLength);
    headers.set("x-content-type-options", "nosniff");

    return new Response(upstream.body, {
      status: 200,
      headers,
    });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: "Download failed", detail: message },
      { status: 503 },
    );
  }
}
