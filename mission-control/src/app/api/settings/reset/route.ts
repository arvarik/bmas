import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";

/**
 * POST /api/settings/reset
 * Reset all settings overrides to bmas.yaml defaults.
 */
export async function POST(): Promise<NextResponse> {
  try {
    const res = await daemonFetch(`${DAEMON_BASE_URL}/settings/reset`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(process.env.BMAS_API_KEY
          ? { Authorization: `Bearer ${process.env.BMAS_API_KEY}` }
          : {}),
      },
    });
    const data = await res.json().catch(() => ({}));
    return NextResponse.json(data, { status: res.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 503 });
  }
}
