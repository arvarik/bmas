import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/** Return the extracted text for one uploaded task file. */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ taskId: string; fileId: string }> },
): Promise<NextResponse> {
  const { taskId, fileId } = await params;
  const encodedTaskId = encodeURIComponent(taskId);
  const encodedFileId = encodeURIComponent(fileId);

  try {
    const upstream = await fetch(
      `${DAEMON_BASE_URL}/tasks/${encodedTaskId}/files/${encodedFileId}/text`,
      {
        cache: "no-store",
        signal: AbortSignal.timeout(15_000),
      },
    );
    const data: unknown = await upstream.json().catch(() => ({}));
    return NextResponse.json(data, {
      status: upstream.status,
      headers: { "Cache-Control": "private, no-store" },
    });
  } catch (error) {
    const detail = error instanceof Error ? error.message : "Unknown error";
    return NextResponse.json(
      { error: "Preview failed", detail },
      { status: 503, headers: { "Cache-Control": "private, no-store" } },
    );
  }
}
