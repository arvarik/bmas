import { NextResponse } from "next/server";
import { BESZEL_HUB_URL, BESZEL_EMAIL, BESZEL_PASSWORD } from "@/lib/config";

// ── Cached PocketBase auth token ──────────────────────────────────────
// Stored in module scope (server-side only). Re-authenticates when expired.

let cachedToken: string | null = null;
let tokenExpiresAt = 0; // Unix ms

async function getAuthToken(): Promise<string> {
  if (cachedToken && Date.now() < tokenExpiresAt - 60_000) {
    return cachedToken;
  }

  if (!BESZEL_EMAIL || !BESZEL_PASSWORD) {
    throw new Error("BESZEL_EMAIL and BESZEL_PASSWORD are required in .env");
  }

  // PocketBase v0.23+ uses /api/collections/users/auth-with-password
  // Fall back to _superusers for admin-only setups.
  const endpoints = [
    `${BESZEL_HUB_URL}/api/collections/users/auth-with-password`,
    `${BESZEL_HUB_URL}/api/collections/_superusers/auth-with-password`,
  ];

  for (const url of endpoints) {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          identity: BESZEL_EMAIL,
          password: BESZEL_PASSWORD,
        }),
        signal: AbortSignal.timeout(5_000),
      });

      if (!res.ok) continue;

      const data = await res.json();
      cachedToken = data.token;
      // PocketBase tokens last ~14 days. We refresh every 12 hours.
      tokenExpiresAt = Date.now() + 12 * 60 * 60 * 1000;
      return data.token;
    } catch {
      continue;
    }
  }

  throw new Error("Failed to authenticate with Beszel Hub — check BESZEL_EMAIL and BESZEL_PASSWORD");
}

// ── Beszel system record shape ────────────────────────────────────────

interface BeszelInfo {
  cpu?: number;    // CPU %
  mp?: number;     // Memory %
  dp?: number;     // Disk %
  dt?: number;     // Disk total GB
  t?: number;      // Temperature °C (primary sensor)
  u?: number;      // Uptime seconds
  v?: string;      // Beszel agent version
  bb?: number;     // Bandwidth bytes
  ct?: number;     // CPU thread count
  la?: number[];   // Load averages [1m, 5m, 15m]
  sv?: number[];   // [swap_used_mb, swap_total_mb]
}

interface BeszelRecord {
  id: string;
  name: string;
  host: string;
  port: string;
  status: string;
  info?: BeszelInfo;
  updated: string;
}

// ── GET /api/telemetry ────────────────────────────────────────────────

export async function GET(): Promise<NextResponse> {
  if (!BESZEL_HUB_URL) {
    return NextResponse.json({
      hub_status: "not_configured",
      note: "Monitoring is not configured in bmas.yaml. Add monitoring.beszel_hub to enable.",
      systems: [],
    });
  }

  if (!BESZEL_EMAIL || !BESZEL_PASSWORD) {
    return NextResponse.json({
      hub_status: "no_credentials",
      note: "BESZEL_EMAIL and BESZEL_PASSWORD must be set in .env for telemetry.",
      systems: [],
    });
  }

  try {
    const token = await getAuthToken();
    const systems = await fetchSystems(token);

    if (systems === null) {
      // Token expired — retry once
      cachedToken = null;
      tokenExpiresAt = 0;
      const freshToken = await getAuthToken();
      const retry = await fetchSystems(freshToken);
      if (retry === null) {
        return NextResponse.json(
          { error: "Beszel auth failed after retry", systems: [] },
          { status: 401 },
        );
      }
      return NextResponse.json({ hub_status: "connected", systems: retry });
    }

    return NextResponse.json({ hub_status: "connected", systems });
  } catch (err) {
    cachedToken = null;
    tokenExpiresAt = 0;
    const message = err instanceof Error ? err.message : "Unknown error";
    return NextResponse.json(
      { error: "Beszel Hub unreachable", detail: message, systems: [] },
      { status: 503 },
    );
  }
}

// ── Fetch + transform ─────────────────────────────────────────────────

async function fetchSystems(token: string) {
  const res = await fetch(
    `${BESZEL_HUB_URL}/api/collections/systems/records?perPage=50`,
    {
      headers: { Authorization: token },
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    },
  );

  if (res.status === 401 || res.status === 403) return null;
  if (!res.ok) throw new Error(`Beszel returned ${res.status}`);

  const data = await res.json();
  const records: BeszelRecord[] = data.items ?? [];

  return records.map((sys) => {
    const i = sys.info ?? {};
    return {
      id: sys.id,
      name: sys.name,
      host: sys.host,
      status: sys.status,
      updatedAt: sys.updated,
      cpu: i.cpu ?? 0,
      memPct: i.mp ?? 0,
      diskPct: i.dp ?? 0,
      diskTotalGB: i.dt ?? 0,
      temp: i.t ?? null,
      uptimeSec: i.u ?? 0,
      agentVersion: i.v ?? null,
      bandwidthBytes: i.bb ?? 0,
      cpuThreads: i.ct ?? 0,
      loadAvg: i.la ?? [],
    };
  });
}
