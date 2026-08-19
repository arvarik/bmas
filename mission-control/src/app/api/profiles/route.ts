import { AGENT_HOSTS, NODES } from "@/lib/config";
import { requestHermesAgent } from "@/lib/hermes-agent-api";

/**
 * GET /api/profiles
 *
 * Return the active API-server profile for each agent node.
 * Hermes selects a profile through the configured gateway URL and key.
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

async function fetchNodeProfiles(
  role: string,
  name: string,
  host: string,
): Promise<NodeProfile> {
  const result: NodeProfile = {
    role,
    name,
    host,
    profiles: [],
    reachable: false,
  };

  try {
    const body = await requestHermesAgent(role, "/v1/capabilities");
    const capability = typeof body === "object" && body !== null
      ? body as Record<string, unknown>
      : {};
    const model = typeof capability.model === "string"
      ? capability.model
      : role;
    result.reachable = true;
    result.profiles = [{
      name: model,
      model,
      is_default: true,
      gateway_running: true,
      description: "Active Hermes API-server profile",
    }];
  } catch {
    return result;
  }

  return result;
}

export async function GET(): Promise<Response> {
  const promises = NODES.map((node) => {
    if (!AGENT_HOSTS[node.role]) {
      return Promise.resolve({
        role: node.role,
        name: node.name,
        host: node.host,
        profiles: [],
        reachable: false,
      } as NodeProfile);
    }
    return fetchNodeProfiles(node.role, node.name, node.host);
  });

  const nodes = await Promise.all(promises);

  return Response.json(
    { nodes },
    { headers: { "Cache-Control": "no-store" } },
  );
}
