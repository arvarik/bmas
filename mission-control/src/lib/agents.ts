/**
 * Agent node data — shared by the Agents list and the agent detail page.
 *
 * The list reads `/api/profiles`. The detail page adds `/api/skills` and
 * `/api/toolsets` for one node.
 */

import {
  failureFromReason,
  failureFromResponse,
  type RequestFailure,
} from "@/lib/request-state";

export interface AgentSkill {
  name: string;
  description?: string;
  category?: string;
  enabled?: boolean;
}

export interface AgentToolset {
  name: string;
  label?: string;
  description?: string;
  enabled?: boolean;
  configured?: boolean;
  tools?: string[];
}

export interface AgentProfileInfo {
  name: string;
  model?: string;
  is_default?: boolean;
  gateway_running?: boolean;
  description?: string;
  distribution_version?: string | null;
}

export interface AgentHealthInfo {
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

export interface AgentNode {
  role: string;
  name: string;
  host: string;
  profiles: AgentProfileInfo[];
  reachable: boolean;
  health?: AgentHealthInfo;
}

export interface AgentCollection<T> {
  items: T[];
  failure: RequestFailure | null;
}

export type AgentStatusTone = "ready" | "degraded" | "unavailable";

export function agentStatus(node: AgentNode): { tone: AgentStatusTone; label: string } {
  if (!node.reachable) return { tone: "unavailable", label: "Unavailable" };
  if (node.health?.ready) return { tone: "ready", label: "Ready" };
  return { tone: "degraded", label: "Degraded" };
}

/** A short engine name, for example "Hermes 0.20.4" or "LiteLLM starter". */
export function agentEngine(node: AgentNode): { label: string; detail: string } {
  const backend = node.health?.execution_backend ?? "";
  const version = node.health?.hermes_version ?? node.profiles[0]?.distribution_version ?? null;
  if (backend.startsWith("hermes")) {
    return {
      label: version ? `Hermes ${version}` : "Hermes",
      detail: backend === "hermes-runs-api" ? "Runs API" : backend === "hermes-cli" ? "CLI" : backend,
    };
  }
  if (backend === "litellm") return { label: "LiteLLM starter", detail: "Direct gateway, no tools" };
  if (!node.reachable) return { label: "Unknown", detail: "No response" };
  return { label: backend || "Unknown engine", detail: "" };
}

export function capacityLabel(node: AgentNode): string {
  const capacity = node.health?.capacity;
  if (capacity === "busy") return "Busy";
  if (capacity === "available") return "Available";
  if (capacity === "ready") return "Ready, usage not reported";
  return "Unavailable";
}

export function roleTitle(role: string): string {
  return role.replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function recordArray<T>(value: unknown, field: string): T[] {
  if (typeof value !== "object" || value === null) return [];
  const items = (value as Record<string, unknown>)[field];
  return Array.isArray(items) ? items as T[] : [];
}

export async function loadAgentNodes(signal?: AbortSignal): Promise<AgentNode[]> {
  const response = await fetch("/api/profiles", {
    cache: "no-store",
    signal: signal ?? AbortSignal.timeout(8_000),
  });
  if (!response.ok) throw await failureFromResponse(response, "Agent discovery failed");
  const body = await response.json() as { nodes?: AgentNode[] };
  return body.nodes ?? [];
}

export async function loadAgentCollection<T>(
  url: string,
  field: string,
): Promise<AgentCollection<T>> {
  try {
    const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(8_000) });
    if (!response.ok) throw await failureFromResponse(response, `${field} request failed`);
    const body = await response.json();
    return { items: recordArray<T>(body, field), failure: null };
  } catch (reason) {
    return { items: [], failure: failureFromReason(reason, `${field} request failed`) };
  }
}

export function loadAgentSkills(role: string): Promise<AgentCollection<AgentSkill>> {
  return loadAgentCollection<AgentSkill>(`/api/skills?node=${encodeURIComponent(role)}`, "skills");
}

export function loadAgentToolsets(role: string): Promise<AgentCollection<AgentToolset>> {
  return loadAgentCollection<AgentToolset>(`/api/toolsets?node=${encodeURIComponent(role)}`, "toolsets");
}
