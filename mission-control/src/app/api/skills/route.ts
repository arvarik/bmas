import { NextResponse } from "next/server";

/** Map of agent names to their LXC container IPs. */
const AGENT_HOSTS: Record<string, string> = {
  planner: "http://192.168.4.103:8000",
  executor: "http://192.168.4.112:8000",
  auditor: "http://192.168.4.122:8000",
};

type AgentName = keyof typeof AGENT_HOSTS;

/** Resolve the upstream URL for a skills request. */
function resolveUpstream(
  node: string | null,
  path: string,
): string | null {
  if (!node || !(node in AGENT_HOSTS)) {
    return null;
  }
  return `${AGENT_HOSTS[node as AgentName]}${path}`;
}

/** GET /api/skills?node=planner — list skills for the given agent. */
export async function GET(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  const node = searchParams.get("node");

  const upstream = resolveUpstream(node, "/skills");

  if (!upstream) {
    return NextResponse.json(
      {
        error: `Invalid or missing 'node' parameter. Expected: ${Object.keys(AGENT_HOSTS).join(" | ")}`,
      },
      { status: 400 },
    );
  }

  try {
    const res = await fetch(upstream, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });

    if (!res.ok) {
      return NextResponse.json(
        { error: `Agent returned ${res.status}` },
        { status: res.status },
      );
    }

    const data: unknown = await res.json();
    return NextResponse.json(data);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: `Agent '${node}' unreachable`, detail: message },
      { status: 503 },
    );
  }
}

/**
 * DELETE /api/skills?node=executor&skill=some_skill_name
 * Forward a skill deletion to the target agent.
 */
export async function DELETE(request: Request): Promise<NextResponse> {
  const { searchParams } = new URL(request.url);
  const node = searchParams.get("node");
  const skill = searchParams.get("skill");

  if (!skill) {
    return NextResponse.json(
      { error: "Missing required 'skill' query parameter" },
      { status: 400 },
    );
  }

  const upstream = resolveUpstream(
    node,
    `/skills/${encodeURIComponent(skill)}`,
  );

  if (!upstream) {
    return NextResponse.json(
      {
        error: `Invalid or missing 'node' parameter. Expected: ${Object.keys(AGENT_HOSTS).join(" | ")}`,
      },
      { status: 400 },
    );
  }

  try {
    const res = await fetch(upstream, {
      method: "DELETE",
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
    });

    if (!res.ok) {
      return NextResponse.json(
        { error: `Agent returned ${res.status}` },
        { status: res.status },
      );
    }

    const data: unknown = await res.json().catch(() => ({ ok: true }));
    return NextResponse.json(data);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: `Agent '${node}' unreachable`, detail: message },
      { status: 503 },
    );
  }
}
