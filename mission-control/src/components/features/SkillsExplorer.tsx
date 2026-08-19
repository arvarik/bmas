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
import { Skeleton } from "@/components/ui/Skeleton";
import { authorColor } from "@/lib/design-tokens";

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

interface CapabilityData {
  skills: Skill[];
  toolsets: Toolset[];
  error: string | null;
}

function recordArray<T>(value: unknown, field: string): T[] {
  if (typeof value !== "object" || value === null) return [];
  const items = (value as Record<string, unknown>)[field];
  return Array.isArray(items) ? items as T[] : [];
}

async function responseError(response: Response): Promise<string> {
  const body = await response.json().catch(() => ({})) as { error?: string };
  return body.error ?? `HTTP ${response.status}`;
}

export default function SkillsExplorer() {
  const [nodes, setNodes] = useState<NodeData[]>([]);
  const [capabilities, setCapabilities] = useState<Record<string, CapabilityData>>({});
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const nodeResponse = await fetch("/api/profiles", { cache: "no-store" });
      if (!nodeResponse.ok) throw new Error(await responseError(nodeResponse));
      const nodeBody = await nodeResponse.json() as { nodes?: NodeData[] };
      const nextNodes = nodeBody.nodes ?? [];
      setNodes(nextNodes);
      setExpandedNodes((current) => current.size
        ? current
        : new Set(nextNodes.map((node) => node.role)));

      const entries = await Promise.all(nextNodes.map(async (node) => {
        try {
          const encodedRole = encodeURIComponent(node.role);
          const [skillResponse, toolsetResponse] = await Promise.all([
            fetch(`/api/skills?node=${encodedRole}`, { cache: "no-store" }),
            fetch(`/api/toolsets?node=${encodedRole}`, { cache: "no-store" }),
          ]);
          if (!skillResponse.ok) throw new Error(await responseError(skillResponse));
          if (!toolsetResponse.ok) throw new Error(await responseError(toolsetResponse));
          const [skillBody, toolsetBody] = await Promise.all([
            skillResponse.json(),
            toolsetResponse.json(),
          ]);
          return [node.role, {
            skills: recordArray<Skill>(skillBody, "skills"),
            toolsets: recordArray<Toolset>(toolsetBody, "toolsets"),
            error: null,
          }] as const;
        } catch (error) {
          return [node.role, {
            skills: [],
            toolsets: [],
            error: error instanceof Error ? error.message : "Capability request failed",
          }] as const;
        }
      }));
      setCapabilities(Object.fromEntries(entries));
    } catch (error) {
      setNodes([]);
      setCapabilities({
        _global: {
          skills: [],
          toolsets: [],
          error: error instanceof Error ? error.message : "Agent discovery failed",
        },
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void Promise.resolve().then(load);
  }, [load]);

  const normalizedFilter = filter.trim().toLowerCase();
  const globalError = capabilities._global?.error;

  const visibleNodes = useMemo(() => nodes.map((node) => {
    const data = capabilities[node.role] ?? { skills: [], toolsets: [], error: null };
    if (!normalizedFilter) return { node, data };
    const skills = data.skills.filter((skill) =>
      [skill.name, skill.description, skill.category]
        .some((value) => typeof value === "string" && value.toLowerCase().includes(normalizedFilter)));
    const toolsets = data.toolsets.filter((toolset) =>
      [toolset.name, toolset.label, toolset.description, ...(toolset.tools ?? [])]
        .some((value) => typeof value === "string" && value.toLowerCase().includes(normalizedFilter)));
    return { node, data: { ...data, skills, toolsets } };
  }), [capabilities, nodes, normalizedFilter]);

  if (loading && nodes.length === 0) {
    return (
      <Panel title="Hermes capabilities">
        <Skeleton variant="list" lines={6} />
      </Panel>
    );
  }

  return (
    <Panel
      title="Hermes capabilities"
      subtitle="Read-only discovery from each routed Hermes API-server profile."
      actions={(
        <ActionButton variant="secondary" onClick={() => void load()} loading={loading}>
          <RefreshCw size={14} /> Refresh
        </ActionButton>
      )}
    >
      <div className="nodes-dashboard">
        <div className="hermes-discovery-note">
          <ShieldCheck size={15} />
          <span>
            Hermes v0.20.4 exposes skill and toolset discovery through read-only routes.
            Change the selected profile configuration on the agent node.
          </span>
        </div>

        <label className="nodes-dashboard__search">
          <Search size={14} aria-hidden="true" />
          <span className="sr-only">Search skills and toolsets</span>
          <input
            type="search"
            placeholder="Search skills, toolsets, or tools"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            className="nodes-dashboard__search-input"
          />
        </label>

        {globalError ? <div className="node-card__error">{globalError}</div> : null}

        <div className="nodes-dashboard__cards">
          {visibleNodes.map(({ node, data }, index) => {
            const expanded = expandedNodes.has(node.role);
            const profile = node.profiles[0];
            const color = authorColor(node.role);
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
                    <span className="node-card__name">Node {index + 1} · {node.name}</span>
                    <span className="node-card__host">{node.host}</span>
                  </span>
                  <span className="node-card__status">
                    {node.reachable ? <Wifi size={13} /> : <WifiOff size={13} />}
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
                        <span className="node-card__profile-dot" style={{ background: color }} />
                        <select value={profile?.name ?? node.role} disabled aria-label={`${node.name} active Hermes profile`}>
                          <option>{profile?.name ?? node.role}</option>
                        </select>
                        <span className="capability-badge capability-badge--enabled">routed key</span>
                      </div>
                      <span className="node-card__empty-hint">
                        The node URL and its profile key select this profile.
                      </span>
                    </section>

                    {data.error ? (
                      <div className="node-card__error">{data.error}</div>
                    ) : (
                      <>
                        <CapabilitySection
                          icon={<Sparkles size={13} />}
                          title="Skills"
                          count={data.skills.length}
                        >
                          {data.skills.length ? data.skills.map((skill) => (
                            <div key={skill.name} className="capability-row">
                              <div className="capability-row__content">
                                <strong>{skill.name}</strong>
                                {skill.description ? <span>{skill.description}</span> : null}
                              </div>
                              <span className={`capability-badge ${skill.enabled === false ? "" : "capability-badge--enabled"}`}>
                                {skill.enabled === false ? "disabled" : skill.category ?? "available"}
                              </span>
                            </div>
                          )) : <span className="node-card__empty-hint">No matching skills</span>}
                        </CapabilitySection>

                        <CapabilitySection
                          icon={<Wrench size={13} />}
                          title="Toolsets"
                          count={data.toolsets.length}
                        >
                          {data.toolsets.length ? data.toolsets.map((toolset) => (
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
                          )) : <span className="node-card__empty-hint">No matching toolsets</span>}
                        </CapabilitySection>
                      </>
                    )}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
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
