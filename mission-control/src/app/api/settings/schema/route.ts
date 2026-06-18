import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";

/**
 * GET /api/settings/schema
 * Returns available models, node hosts, complexity tiers, and known roles.
 */
export async function GET(): Promise<NextResponse> {
  try {
    const res = await fetch(`${DAEMON_BASE_URL}/settings/schema`, {
      cache: "no-store",
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      return NextResponse.json(
        { error: `Daemon returned ${res.status}`, detail },
        { status: res.status }
      );
    }
    const data = await res.json();
    return NextResponse.json(data);
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json({ error: message }, { status: 503 });
  }
}
