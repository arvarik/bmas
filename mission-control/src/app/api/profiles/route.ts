import { NextResponse } from "next/server";
import { AGENT_DASHBOARD_HOSTS, NODES } from "@/lib/config";

/**
 * GET /api/profiles
 *
 * Returns profile metadata for each agent node.
 * Proxies to each node's Hermes Dashboard:
 *   - GET :9119/api/profiles → list of installed profiles with metadata
 *
 * If a node's dashboard is unreachable, that node is returned with
 * empty profiles and `reachable: false`.
 */

interface ProfileInfo {
  name: string;
  path?: string;
  is_default?: boolean;
  model?: string;
  provider?: string;
  has_env?: boolean;
  skill_count?: number;
  gateway_running?: boolean;
  description?: string;
  distribution_name?: string | null;
  distribution_version?: string | null;
  distribution_source?: string | null;
  has_alias?: boolean;
}

interface NodeProfile {
  role: string;
  name: string;
  host: string;
  profiles: ProfileInfo[];
  reachable: boolean;
}

/**
 * Fetch the ephemeral session token from the Hermes Dashboard.
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

async function fetchNodeProfiles(
  role: string,
  name: string,
  host: string,
  baseUrl: string,
): Promise<NodeProfile> {
  const result: NodeProfile = {
    role,
    name,
    host,
    profiles: [],
    reachable: false,
  };

  const token = await fetchSessionToken(baseUrl);
  if (!token) return result;

  result.reachable = true;

  // Fetch profiles
  try {
    const res = await fetch(`${baseUrl}/api/profiles`, {
      cache: "no-store",
      signal: AbortSignal.timeout(5_000),
      headers: { "X-Hermes-Session-Token": token },
    });
    if (res.ok) {
      const data: unknown = await res.json();
      result.profiles = Array.isArray(data)
        ? data
        : (data as { profiles?: ProfileInfo[] })?.profiles ?? [];
    }
  } catch {
    // Profiles endpoint may not exist in this Hermes version
  }

  return result;
}

export async function GET(): Promise<NextResponse> {
  // Query all nodes in parallel
  const promises = NODES.map((node) => {
    const baseUrl = AGENT_DASHBOARD_HOSTS[node.role];
    if (!baseUrl) {
      return Promise.resolve({
        role: node.role,
        name: node.name,
        host: node.host,
        profiles: [],
        reachable: false,
      } as NodeProfile);
    }
    return fetchNodeProfiles(node.role, node.name, node.host, baseUrl);
  });

  const nodes = await Promise.all(promises);

  return NextResponse.json(
    { nodes },
    { headers: { "Cache-Control": "no-store" } },
  );
}
