import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * PATCH /api/settings/routing
 * Override complexity → model routing for the session.
 */
export async function PATCH(request: Request): Promise<NextResponse> {
  try {
    const body = await request.json();
    const res = await fetch(`${DAEMON_BASE_URL}/settings/routing`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 503 });
  }
}
