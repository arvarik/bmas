import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * POST /api/settings/reset
 * Reset all settings overrides to bmas.yaml defaults.
 */
export async function POST(): Promise<NextResponse> {
  try {
    const res = await fetch(`${DAEMON_BASE_URL}/settings/reset`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 503 });
  }
}
