import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string; fileId: string }> },
): Promise<Response> {
  const { taskId, fileId } = await params;
  try {
    const upstream = await daemonFetch(
      `${DAEMON_BASE_URL}/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(fileId)}`,
      { cache: "no-store", signal: AbortSignal.timeout(15_000) },
    );
    if (!upstream.ok) {
      return NextResponse.json(
        { error: `File preview returned HTTP ${upstream.status}` },
        { status: upstream.status },
      );
    }
    const headers = new Headers({
      "Cache-Control": "private, no-store",
      "Content-Disposition": "inline",
      "X-Content-Type-Options": "nosniff",
    });
    const contentType = upstream.headers.get("content-type");
    const contentLength = upstream.headers.get("content-length");
    if (contentType) headers.set("Content-Type", contentType);
    if (contentLength) headers.set("Content-Length", contentLength);
    return new Response(upstream.body, { status: 200, headers });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "Unknown error";
    return NextResponse.json(
      { error: "File preview failed", detail },
      { status: 503 },
    );
  }
}
