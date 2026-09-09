import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";

async function forward(method: "GET" | "POST", body?: unknown): Promise<NextResponse> {
  try {
    const response = await daemonFetch(`${DAEMON_BASE_URL}/classic/spec/${method === "GET" ? "schema" : "compile"}`, {
      method,
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      ...(method === "POST" ? { body: JSON.stringify(body) } : {}),
      signal: AbortSignal.timeout(15_000),
    });
    return NextResponse.json(await response.json(), {
      status: response.status, headers: { "Cache-Control": "no-store" },
    });
  } catch {
    return NextResponse.json({ detail: "The Classic preview service is unavailable. Retry the preview." }, { status: 503 });
  }
}

export async function GET(): Promise<NextResponse> {
  return forward("GET");
}

export async function POST(request: Request): Promise<NextResponse> {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: { errors: [{ field: "advanced", message: "Enter a valid JSON object." }] } }, { status: 400 });
  }
  return forward("POST", body);
}
