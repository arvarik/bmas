import { NextResponse } from "next/server";
import { DAEMON_SUBMIT_URL } from "@/lib/config";

interface SubmitPayload {
  task: string;
}

export async function POST(request: Request): Promise<NextResponse> {
  try {
    const body = (await request.json()) as SubmitPayload;

    if (!body.task || typeof body.task !== "string" || !body.task.trim()) {
      return NextResponse.json(
        { error: "Missing or empty 'task' field" },
        { status: 400 },
      );
    }

    const upstream = await fetch(DAEMON_SUBMIT_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ task: body.task.trim() }),
      signal: AbortSignal.timeout(5_000), // daemon responds immediately (HTTP 202)
    });

    if (!upstream.ok) {
      const detail = await upstream.text().catch(() => "");
      return NextResponse.json(
        { error: `Daemon returned ${upstream.status}`, detail },
        { status: upstream.status },
      );
    }

    const data: unknown = await upstream.json();
    return NextResponse.json(data, { status: upstream.status });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: "Task submission failed", detail: message },
      { status: 503 },
    );
  }
}
