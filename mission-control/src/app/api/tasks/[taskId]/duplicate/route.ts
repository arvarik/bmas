import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

export async function POST(
  _request: Request,
  { params }: { params: Promise<{ taskId: string }> },
): Promise<NextResponse> {
  const { taskId } = await params;
  try {
    const upstream = await fetch(
      `${DAEMON_BASE_URL}/tasks/${encodeURIComponent(taskId)}/duplicate`,
      {
        method: "POST",
        headers: process.env.BMAS_API_KEY
          ? { Authorization: `Bearer ${process.env.BMAS_API_KEY}` }
          : {},
        signal: AbortSignal.timeout(120_000),
      },
    );
    return NextResponse.json(await upstream.json(), { status: upstream.status });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Task duplication failed",
        detail: error instanceof Error ? error.message : "Unknown upstream error",
      },
      { status: 503 },
    );
  }
}
