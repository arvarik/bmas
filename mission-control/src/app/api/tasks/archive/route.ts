import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

export async function POST(request: Request): Promise<NextResponse> {
  try {
    const body = await request.text();
    const upstream = await fetch(`${DAEMON_BASE_URL}/tasks/archive`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(process.env.BMAS_API_KEY
          ? { Authorization: `Bearer ${process.env.BMAS_API_KEY}` }
          : {}),
      },
      body,
      signal: AbortSignal.timeout(10_000),
    });
    return NextResponse.json(await upstream.json(), { status: upstream.status });
  } catch (error) {
    return NextResponse.json(
      {
        error: "Task archive request failed",
        detail: error instanceof Error ? error.message : "Unknown upstream error",
      },
      { status: 503 },
    );
  }
}
