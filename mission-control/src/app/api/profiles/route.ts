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

interface NodeHealthInfo {
  status: "ready" | "degraded" | "unavailable";
  ready: boolean;
  capacity: "available" | "busy" | "ready" | "unavailable";
  current_task: string | null;
  current_task_reported: boolean;
  model: string | null;
  hermes_version: string | null;
  hermes_status: string | null;
  execution_backend: string | null;
  runs_api_ready: boolean;
}

interface NodeProfile {
  role: string;
  name: string;
  host: string;
  profiles: ProfileInfo[];
  reachable: boolean;
  health: NodeHealthInfo;
}

const UNAVAILABLE_HEALTH: NodeHealthInfo = {
  status: "unavailable",
  ready: false,
  capacity: "unavailable",
  current_task: null,
  current_task_reported: false,
  model: null,
  hermes_version: null,
  hermes_status: null,
  execution_backend: null,
  runs_api_ready: false,
};

function asRecord(value: unknown): Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
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
    health: UNAVAILABLE_HEALTH,
  };

  const [capabilityResult, healthResult] = await Promise.allSettled([
    requestHermesAgent(role, "/v1/capabilities"),
    requestHermesAgent(role, "/health/detailed"),
  ]);
  if (capabilityResult.status === "rejected" && healthResult.status === "rejected") {
    return result;
  }

  const capability = capabilityResult.status === "fulfilled"
    ? asRecord(capabilityResult.value)
    : {};
  const health = healthResult.status === "fulfilled"
    ? asRecord(healthResult.value)
    : {};
  const currentTask = optionalString(health.current_task);
  const currentTaskReported = Object.prototype.hasOwnProperty.call(health, "current_task");
  const ready = health.ready === true;
  const model = optionalString(health.model) ?? optionalString(capability.model) ?? role;
  const hermesVersion = optionalString(health.hermes_version)
    ?? optionalString(capability.version);

  result.reachable = true;
  result.health = {
    status: ready ? "ready" : "degraded",
    ready,
    capacity: currentTask
      ? "busy"
      : currentTaskReported && ready
        ? "available"
        : ready ? "ready" : "unavailable",
    current_task: currentTask,
    current_task_reported: currentTaskReported,
    model,
    hermes_version: hermesVersion,
    hermes_status: optionalString(health.hermes_status),
    execution_backend: optionalString(health.execution_backend),
    runs_api_ready: health.runs_api_ready === true,
  };
  result.profiles = [{
    name: model,
    model,
    is_default: true,
    gateway_running: health.runs_api_ready !== false,
    description: "Active Hermes API-server profile",
    distribution_version: hermesVersion,
  }];

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
        health: UNAVAILABLE_HEALTH,
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
