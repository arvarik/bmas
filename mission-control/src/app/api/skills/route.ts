import { NextResponse } from "next/server";
import { AGENT_DASHBOARD_HOSTS } from "@/lib/config";

type AgentRole = keyof typeof AGENT_DASHBOARD_HOSTS;


/**
 * Fetch the ephemeral session token from the Hermes Dashboard.
 *
 * The Hermes Dashboard injects a session token into the HTML page on load:
 *   window.__HERMES_SESSION_TOKEN__="<token>";
 *
 * This token is generated per-process via `secrets.token_urlsafe(32)`.
 * It must be sent as `X-Hermes-Session-Token` header on all API requests.
 */
async function fetchSessionToken(baseUrl: string): Promise<string | null> {
  try {
    const res = await fetch(baseUrl, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });
    if (!res.ok) return null;

    const html = await res.text();
    const match = html.match(/__HERMES_SESSION_TOKEN__="([^"]+)"/);
    return match?.[1] ?? null;
  } catch {
    return null;
  }
}

/**
 * GET /api/skills?node=planner — list skills for the given agent.
 *
 * Proxies to Hermes Dashboard: GET :9119/api/skills
 * Handles session token authentication automatically.
 */
export async function GET(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  const node = searchParams.get("node");

  if (!node || !(node in AGENT_DASHBOARD_HOSTS)) {
    return NextResponse.json(
      {
        error: `Invalid or missing 'node' parameter. Expected: ${Object.keys(AGENT_DASHBOARD_HOSTS).join(" | ")}`,
      },
      { status: 400 },
    );
  }

  const baseUrl = AGENT_DASHBOARD_HOSTS[node as AgentRole];

  // 1. Fetch the session token from the dashboard HTML
  const token = await fetchSessionToken(baseUrl);
  if (!token) {
    return NextResponse.json(
      { error: `Hermes Dashboard for '${node}' unreachable or returned no session token` },
      { status: 503 },
    );
  }

  // 2. Fetch skills using the session token
  try {
    const res = await fetch(`${baseUrl}/api/skills`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
      headers: {
        "X-Hermes-Session-Token": token,
      },
    });

    if (!res.ok) {
      // 404 means the dashboard version doesn't have the skills endpoint.
      if (res.status === 404) {
        return NextResponse.json({ skills: [] });
      }
      return NextResponse.json(
        { error: `Hermes Dashboard returned ${res.status}` },
        { status: res.status },
      );
    }

    // Hermes returns a flat array: [{ name, description, category, enabled }]
    // Wrap in { skills: [...] } for the frontend.
    const data: unknown = await res.json();
    const skills = Array.isArray(data) ? data : [];
    return NextResponse.json({ skills });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: `Hermes Dashboard for '${node}' unreachable`, detail: message },
      { status: 503 },
    );
  }
}

/**
 * PUT /api/skills — toggle a skill on/off.
 *
 * Body: { node: "planner", name: "skill-name", enabled: true }
 * Proxies to Hermes Dashboard: PUT :9119/api/skills/toggle
 */
export async function PUT(request: Request): Promise<NextResponse> {
  let body: { node?: string; name?: string; enabled?: boolean };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json(
      { error: "Invalid JSON body" },
      { status: 400 },
    );
  }

  const { node, name, enabled } = body;
  if (!node || !name || typeof enabled !== "boolean") {
    return NextResponse.json(
      { error: "Required fields: node, name, enabled" },
      { status: 400 },
    );
  }

  if (!(node in AGENT_DASHBOARD_HOSTS)) {
    return NextResponse.json(
      { error: `Invalid node. Expected: ${Object.keys(AGENT_DASHBOARD_HOSTS).join(" | ")}` },
      { status: 400 },
    );
  }

  const baseUrl = AGENT_DASHBOARD_HOSTS[node as AgentRole];
  const token = await fetchSessionToken(baseUrl);
  if (!token) {
    return NextResponse.json(
      { error: `Hermes Dashboard for '${node}' unreachable` },
      { status: 503 },
    );
  }

  try {
    const res = await fetch(`${baseUrl}/api/skills/toggle`, {
      method: "PUT",
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
      headers: {
        "X-Hermes-Session-Token": token,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ name, enabled }),
    });

    if (!res.ok) {
      return NextResponse.json(
        { error: `Hermes Dashboard returned ${res.status}` },
        { status: res.status },
      );
    }

    const data: unknown = await res.json().catch(() => ({ ok: true }));
    return NextResponse.json(data);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: `Hermes Dashboard for '${node}' unreachable`, detail: message },
      { status: 503 },
    );
  }
}
