"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  Cpu,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Wifi,
  WifiOff,
  Wrench,
} from "lucide-react";
import { ActionButton } from "@/components/ui/ActionButton";
import { Panel } from "@/components/ui/Panel";
import { ResourceState } from "@/components/ui/ResourceState";
import { Skeleton } from "@/components/ui/Skeleton";
import { authorColor } from "@/lib/design-tokens";
import {
  diagnosticsText,
  failureFromReason,
  failureFromResponse,
  type RequestFailure,
} from "@/lib/request-state";

interface Skill {
  name: string;
  description?: string;
  category?: string;
  enabled?: boolean;
}

interface Toolset {
  name: string;
  label?: string;
  description?: string;
  enabled?: boolean;
  configured?: boolean;
  tools?: string[];
}

interface ProfileInfo {
  name: string;
  model?: string;
  is_default?: boolean;
  gateway_running?: boolean;
  description?: string;
}

interface NodeData {
  role: string;
  name: string;
  host: string;
  profiles: ProfileInfo[];
  reachable: boolean;
}

interface CapabilityCollection<T> {
  items: T[];
  failure: RequestFailure | null;
}

interface CapabilityData {
  skills: CapabilityCollection<Skill>;
  toolsets: CapabilityCollection<Toolset>;
}

function recordArray<T>(value: unknown, field: string): T[] {
  if (typeof value !== "object" || value === null) return [];
  const items = (value as Record<string, unknown>)[field];
  return Array.isArray(items) ? items as T[] : [];
}

async function loadCapability<T>(
  url: string,
  field: string,
): Promise<CapabilityCollection<T>> {
  try {
    const response = await fetch(url, { cache: "no-store", signal: AbortSignal.timeout(8_000) });
    if (!response.ok) throw await failureFromResponse(response, `${field} request failed`);
    const body = await response.json();
    return { items: recordArray<T>(body, field), failure: null };
  } catch (reason) {
    return { items: [], failure: failureFromReason(reason, `${field} request failed`) };
  }
}

export default function SkillsExplorer() {
  const [nodes, setNodes] = useState<NodeData[]>([]);
  const [capabilities, setCapabilities] = useState<Record<string, CapabilityData>>({});
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");
  const [globalFailure, setGlobalFailure] = useState<RequestFailure | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setGlobalFailure(null);
    try {
      const nodeResponse = await fetch("/api/profiles", {
        cache: "no-store",
        signal: AbortSignal.timeout(8_000),
      });
      if (!nodeResponse.ok) throw await failureFromResponse(nodeResponse, "Agent discovery failed");
      const nodeBody = await nodeResponse.json() as { nodes?: NodeData[] };
      const nextNodes = nodeBody.nodes ?? [];
      setNodes(nextNodes);
      setExpandedNodes((current) => current.size
        ? current
        : new Set(nextNodes.map((node) => node.role)));

      const entries = await Promise.all(nextNodes.map(async (node) => {
        if (!node.reachable) {
          const unavailable: RequestFailure = {
            kind: "unavailable",
            message: `${node.name} is unreachable`,
            detail: `No response came from ${node.host}.`,
          };
          return [node.role, {
            skills: { items: [], failure: unavailable },
            toolsets: { items: [], failure: unavailable },
          }] as const;
        }

        const encodedRole = encodeURIComponent(node.role);
        const [skills, toolsets] = await Promise.all([
          loadCapability<Skill>(`/api/skills?node=${encodedRole}`, "skills"),
          loadCapability<Toolset>(`/api/toolsets?node=${encodedRole}`, "toolsets"),
        ]);
        return [node.role, { skills, toolsets }] as const;
      }));
      setCapabilities(Object.fromEntries(entries));
    } catch (reason) {
      setNodes([]);
      setCapabilities({});
      setGlobalFailure(failureFromReason(reason, "Agent discovery failed"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const normalizedFilter = filter.trim().toLowerCase();

  const visibleNodes = useMemo(() => nodes.map((node) => {
    const data = capabilities[node.role] ?? {
      skills: { items: [], failure: null },
      toolsets: { items: [], failure: null },
    };
    const capabilityLoading = loading && !capabilities[node.role];
    if (!normalizedFilter) return { node, data, matches: true, capabilityLoading };
    const skillItems = data.skills.items.filter((skill) =>
      [skill.name, skill.description, skill.category]
        .some((value) => typeof value === "string" && value.toLowerCase().includes(normalizedFilter)));
    const toolsetItems = data.toolsets.items.filter((toolset) =>
      [toolset.name, toolset.label, toolset.description, ...(toolset.tools ?? [])]
        .some((value) => typeof value === "string" && value.toLowerCase().includes(normalizedFilter)));
    const nodeMatches = [node.role, node.name, node.host, node.profiles[0]?.name]
      .some((value) => typeof value === "string" && value.toLowerCase().includes(normalizedFilter));
    return {
      node,
      data: {
        skills: { ...data.skills, items: skillItems },
        toolsets: { ...data.toolsets, items: toolsetItems },
      },
      matches: nodeMatches || skillItems.length > 0 || toolsetItems.length > 0,
      capabilityLoading,
    };
  }).filter((entry) => !normalizedFilter || entry.matches), [capabilities, loading, nodes, normalizedFilter]);

  const unreachableNodes = nodes.filter((node) => !node.reachable);

  if (loading && nodes.length === 0) {
    return (
      <Panel title="Agents" subtitle="Discovering agent profiles and capabilities.">
        <Skeleton variant="list" lines={6} />
      </Panel>
    );
  }

  return (
    <Panel
      title="Agents"
      subtitle="Inspect each routed Hermes profile, its connection state, and its read-only capabilities."
      actions={(
        <ActionButton variant="secondary" onClick={() => void load()} loading={loading}>
          <RefreshCw size={14} /> Refresh
        </ActionButton>
      )}
    >
      <div className="nodes-dashboard">
        {globalFailure ? (
          <ResourceState
            kind={globalFailure.kind}
            title={globalFailure.kind === "permission" ? "Agent discovery access denied" : "Agent discovery failed"}
            description="Mission Control cannot load agent profiles or capabilities."
            detail={globalFailure.detail}
            diagnostics={diagnosticsText("Agent discovery", globalFailure)}
            onRetry={load}
            operationsHref="/infra"
          />
        ) : null}

        {!globalFailure && nodes.length === 0 ? (
          <ResourceState
            kind="unavailable"
            title="No agent nodes are configured"
            description="Add at least one agent node before you inspect profiles, skills, or toolsets."
            detail="The profiles endpoint returned an empty node list."
            diagnostics={JSON.stringify({
              component: "Agent discovery",
              state: "no_nodes",
              captured_at: new Date().toISOString(),
            }, null, 2)}
            onRetry={load}
            operationsHref="/infra"
          />
        ) : null}

        {!globalFailure && nodes.length > 0 ? (
          <>
            <div className="hermes-discovery-note">
              <ShieldCheck size={15} />
              <span>
                Mission Control reads skills and toolsets from each routed Hermes profile.
                Change profile configuration on the agent node.
              </span>
            </div>

            {unreachableNodes.length === nodes.length ? (
              <ResourceState
                kind="unavailable"
                title="All agent nodes are unavailable"
                description="Capabilities remain unavailable until at least one configured agent responds."
                detail={unreachableNodes.map((node) => `${node.name}: ${node.host}`).join("\n")}
                diagnostics={JSON.stringify({
                  component: "Agent discovery",
                  state: "all_nodes_unavailable",
                  nodes: unreachableNodes.map(({ role, name, host }) => ({ role, name, host })),
                  captured_at: new Date().toISOString(),
                }, null, 2)}
                onRetry={load}
                operationsHref="/infra"
                compact
              />
            ) : null}

            <label className="nodes-dashboard__search">
              <Search size={14} aria-hidden="true" />
              <span className="sr-only">Search agents, skills, and toolsets</span>
              <input
                type="search"
                placeholder="Search agents, skills, toolsets, or tools"
                value={filter}
                onChange={(event) => setFilter(event.target.value)}
                className="nodes-dashboard__search-input"
              />
            </label>

            {!loading && normalizedFilter && visibleNodes.length === 0 ? (
              <ResourceState
                kind="empty"
                title="No capabilities match this search"
                description="Change the search term to find another agent, skill, toolset, or tool."
                compact
              />
            ) : null}

            <div className="nodes-dashboard__cards">
          {visibleNodes.map(({ node, data, capabilityLoading }) => {
            const expanded = expandedNodes.has(node.role);
            const profile = node.profiles[0];
            const color = authorColor(node.role);
            const nodeNumber = nodes.findIndex((candidate) => candidate.role === node.role) + 1;
            return (
              <article key={node.role} className="node-card">
                <button
                  type="button"
                  className="node-card__header"
                  onClick={() => setExpandedNodes((current) => {
                    const next = new Set(current);
                    if (next.has(node.role)) next.delete(node.role);
                    else next.add(node.role);
                    return next;
                  })}
                  aria-expanded={expanded}
                >
                  <span
                    className="node-card__dot"
                    style={{ background: node.reachable ? "var(--status-success)" : "var(--status-error)" }}
                  />
                  <span className="node-card__identity">
                    <span className="node-card__name">Node {nodeNumber} · {node.name}</span>
                    <span className="node-card__host">{node.host}</span>
                  </span>
                  <span className="node-card__status">
                    {node.reachable ? <Wifi size={13} /> : <WifiOff size={13} />}
                    <span>{node.reachable ? "Reachable" : "Unavailable"}</span>
                  </span>
                  {expanded ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                </button>

                {expanded ? (
                  <div className="node-card__body">
                    <section className="node-card__section">
                      <div className="node-card__section-header">
                        <Cpu size={13} />
                        <span>Active API profile</span>
                      </div>
                      <div className="hermes-profile-control">
                        <span className="node-card__profile-dot" style={{ background: node.reachable ? color : "var(--status-error)" }} />
                        <select value={profile?.name ?? node.role} disabled aria-label={`${node.name} active Hermes profile`}>
                          <option>{profile?.name ?? node.role}</option>
                        </select>
                        <span className={`capability-badge ${node.reachable ? "capability-badge--enabled" : ""}`}>
                          {node.reachable ? "routed key" : "unavailable"}
                        </span>
                      </div>
                      <span className="node-card__empty-hint">
                        The node URL and its profile key select this profile.
                      </span>
                    </section>

                    {capabilityLoading ? (
                      <Skeleton variant="list" lines={4} />
                    ) : !node.reachable ? (
                      <ResourceState
                        kind="unavailable"
                        title={`${node.name} is unavailable`}
                        description="Skills and toolsets remain unavailable until this node responds."
                        detail={`No response came from ${node.host}.`}
                        compact
                      />
                    ) : (
                      <>
                        <CapabilitySection
                          icon={<Sparkles size={13} />}
                          title="Skills"
                          count={data.skills.items.length}
                        >
                          {data.skills.failure ? (
                            <ResourceState
                              kind={data.skills.failure.kind}
                              title={data.skills.failure.kind === "permission" ? "Skill access denied" : "Skills unavailable"}
                              description="This node did not return its skill catalog."
                              detail={data.skills.failure.detail}
                              diagnostics={diagnosticsText(`${node.name} skills`, data.skills.failure, { role: node.role, host: node.host })}
                              operationsHref="/infra"
                              compact
                            />
                          ) : data.skills.items.length ? data.skills.items.map((skill) => (
                            <div key={skill.name} className="capability-row">
                              <div className="capability-row__content">
                                <strong>{skill.name}</strong>
                                {skill.description ? <span>{skill.description}</span> : null}
                              </div>
                              <span className={`capability-badge ${skill.enabled === false ? "" : "capability-badge--enabled"}`}>
                                {skill.enabled === false ? "disabled" : skill.category ?? "available"}
                              </span>
                            </div>
                          )) : <span className="node-card__empty-hint">
                            {normalizedFilter ? "No matching skills" : "This profile reports no skills"}
                          </span>}
                        </CapabilitySection>

                        <CapabilitySection
                          icon={<Wrench size={13} />}
                          title="Toolsets"
                          count={data.toolsets.items.length}
                        >
                          {data.toolsets.failure ? (
                            <ResourceState
                              kind={data.toolsets.failure.kind}
                              title={data.toolsets.failure.kind === "permission" ? "Toolset access denied" : "Toolsets unavailable"}
                              description="This node did not return its toolset catalog."
                              detail={data.toolsets.failure.detail}
                              diagnostics={diagnosticsText(`${node.name} toolsets`, data.toolsets.failure, { role: node.role, host: node.host })}
                              operationsHref="/infra"
                              compact
                            />
                          ) : data.toolsets.items.length ? data.toolsets.items.map((toolset) => (
                            <div key={toolset.name} className="toolset-card">
                              <div className="toolset-card__header">
                                <div className="capability-row__content">
                                  <strong>{toolset.label || toolset.name}</strong>
                                  {toolset.description ? <span>{toolset.description}</span> : null}
                                </div>
                                <div className="toolset-card__badges">
                                  <span className={`capability-badge ${toolset.configured ? "capability-badge--configured" : ""}`}>
                                    {toolset.configured ? "configured" : "needs config"}
                                  </span>
                                  <span className={`capability-badge ${toolset.enabled ? "capability-badge--enabled" : ""}`}>
                                    {toolset.enabled ? "enabled" : "off"}
                                  </span>
                                </div>
                              </div>
                              {toolset.tools?.length ? (
                                <div className="toolset-card__tools">
                                  {toolset.tools.map((tool) => <code key={tool}>{tool}</code>)}
                                </div>
                              ) : null}
                            </div>
                          )) : <span className="node-card__empty-hint">
                            {normalizedFilter ? "No matching toolsets" : "This profile reports no toolsets"}
                          </span>}
                        </CapabilitySection>
                      </>
                    )}
                  </div>
                ) : null}
              </article>
            );
          })}
            </div>
          </>
        ) : null}
      </div>
    </Panel>
  );
}

function CapabilitySection({
  icon,
  title,
  count,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  count: number;
  children: React.ReactNode;
}) {
  return (
    <section className="node-card__section">
      <div className="node-card__section-header">
        {icon}
        <span>{title}</span>
        <span className="node-card__badge">{count}</span>
      </div>
      <div className="capability-list">{children}</div>
    </section>
  );
}
