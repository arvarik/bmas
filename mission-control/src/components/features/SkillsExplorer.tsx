"use client";

import { useEffect, useState, useCallback } from "react";
import { Panel } from "@/components/ui/Panel";
import { ActionButton } from "@/components/ui/ActionButton";
import { Skeleton } from "@/components/ui/Skeleton";
import { useToast } from "@/hooks/useToast";
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";
import {
  Bot,
  ChevronDown,
  ChevronRight,
  User,
  Wifi,
  WifiOff,
  Search,
  Sparkles,
  Cpu,
  Zap,
} from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface Skill {
  name: string;
  description?: string;
  category?: string;
  enabled?: boolean;
}

interface ProfileInfo {
  name: string;
  is_default?: boolean;
  model?: string;
  provider?: string;
  skill_count?: number;
  gateway_running?: boolean;
  description?: string;
  distribution_name?: string | null;
}

interface NodeData {
  role: string;
  name: string;
  host: string;
  profiles: ProfileInfo[];
  reachable: boolean;
}

const NODE_ROLES: AgentRole[] = ["planner", "executor", "auditor"];
const NODE_INDEX: Record<string, number> = { planner: 1, executor: 2, auditor: 3 };

// ── Main Component ────────────────────────────────────────────────────

export default function SkillsExplorer() {
  // ── Node profile data ──────────────────────────────────────────────
  const [nodeData, setNodeData] = useState<NodeData[]>([]);
  const [nodeDataLoading, setNodeDataLoading] = useState(true);

  // ── Per-node skills ────────────────────────────────────────────────
  const [skillsByNode, setSkillsByNode] = useState<Record<string, Skill[]>>({});
  const [skillsLoading, setSkillsLoading] = useState<Record<string, boolean>>({});
  const [skillsErrors, setSkillsErrors] = useState<Record<string, string | null>>({});

  // ── Expanded sections ──────────────────────────────────────────────
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set(NODE_ROLES));
  const [expandedSkills, setExpandedSkills] = useState<Set<string>>(new Set(NODE_ROLES));

  // ── Filter ─────────────────────────────────────────────────────────
  const [filter, setFilter] = useState("");
  const [togglingSkill, setTogglingSkill] = useState<string | null>(null);

  const { toast } = useToast();

  // ── Fetch node profiles ────────────────────────────────────────────
  const fetchNodeData = useCallback(async () => {
    setNodeDataLoading(true);
    try {
      const res = await fetch("/api/profiles", { cache: "no-store" });
      if (res.ok) {
        const data = (await res.json()) as { nodes: NodeData[] };
        setNodeData(data.nodes ?? []);
      }
    } catch {
      // Best-effort
    } finally {
      setNodeDataLoading(false);
    }
  }, []);

  // ── Fetch skills for a single node ─────────────────────────────────
  const fetchSkills = useCallback(async (role: string) => {
    setSkillsLoading(prev => ({ ...prev, [role]: true }));
    setSkillsErrors(prev => ({ ...prev, [role]: null }));
    try {
      const res = await fetch(`/api/skills?node=${role}`, { cache: "no-store" });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { error?: string };
        setSkillsErrors(prev => ({ ...prev, [role]: body.error ?? `HTTP ${res.status}` }));
        setSkillsByNode(prev => ({ ...prev, [role]: [] }));
        return;
      }
      const data = (await res.json()) as { skills?: Skill[] };
      setSkillsByNode(prev => ({ ...prev, [role]: data.skills ?? [] }));
    } catch (err) {
      setSkillsErrors(prev => ({
        ...prev,
        [role]: err instanceof Error ? err.message : "Fetch failed",
      }));
      setSkillsByNode(prev => ({ ...prev, [role]: [] }));
    } finally {
      setSkillsLoading(prev => ({ ...prev, [role]: false }));
    }
  }, []);

  // ── Initial load ───────────────────────────────────────────────────
  useEffect(() => {
    void fetchNodeData();
    for (const role of NODE_ROLES) {
      void fetchSkills(role);
    }
  }, [fetchNodeData, fetchSkills]);

  // ── Toggle skill ───────────────────────────────────────────────────
  const toggleSkill = async (role: string, skill: Skill) => {
    setTogglingSkill(`${role}:${skill.name}`);
    try {
      const res = await fetch("/api/skills", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          node: role,
          name: skill.name,
          enabled: !skill.enabled,
        }),
      });
      if (res.ok) {
        setSkillsByNode(prev => ({
          ...prev,
          [role]: (prev[role] ?? []).map(s =>
            s.name === skill.name ? { ...s, enabled: !s.enabled } : s
          ),
        }));
        toast({
          type: "success",
          message: `Skill '${skill.name}' ${!skill.enabled ? "enabled" : "disabled"}.`,
        });
      } else {
        toast({ type: "error", message: "Failed to toggle skill." });
      }
    } catch {
      toast({ type: "error", message: "Network error toggling skill." });
    } finally {
      setTogglingSkill(null);
    }
  };

  // ── Toggle helpers ─────────────────────────────────────────────────
  const toggleNode = (role: string) => {
    setExpandedNodes(prev => {
      const next = new Set(prev);
      if (next.has(role)) next.delete(role);
      else next.add(role);
      return next;
    });
  };

  const toggleSkillsSection = (role: string) => {
    setExpandedSkills(prev => {
      const next = new Set(prev);
      if (next.has(role)) next.delete(role);
      else next.add(role);
      return next;
    });
  };

  // ── Merge node data with roles ─────────────────────────────────────
  const getNodeInfo = (role: string): NodeData | null => {
    return nodeData.find(n => n.role === role) ?? null;
  };

  // ── Loading state ──────────────────────────────────────────────────
  if (nodeDataLoading && nodeData.length === 0) {
    return (
      <Panel title="Agents" emptyIcon={Bot} emptyMessage="Loading...">
        <Skeleton variant="list" lines={6} />
      </Panel>
    );
  }

  return (
    <Panel title="Agents" emptyIcon={Bot} emptyMessage="No agents configured">
      <div className="nodes-dashboard">
        {/* Global search */}
        <div className="nodes-dashboard__search">
          <Search size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
          <input
            type="text"
            placeholder="Search skills across all agents..."
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            className="nodes-dashboard__search-input"
          />
        </div>

        {/* Node cards */}
        <div className="nodes-dashboard__cards">
          {NODE_ROLES.map((role) => {
            const nodeInfo = getNodeInfo(role);
            const color = AGENT_COLORS[role];
            const isExpanded = expandedNodes.has(role);
            const isSkillsExpanded = expandedSkills.has(role);
            const skills = skillsByNode[role] ?? [];
            const isSkillsLoading = skillsLoading[role] ?? false;
            const skillsError = skillsErrors[role];
            const reachable = nodeInfo?.reachable ?? false;

            // Filter skills
            const filteredSkills = filter
              ? skills.filter(
                  (s) =>
                    s.name.toLowerCase().includes(filter.toLowerCase()) ||
                    s.description?.toLowerCase().includes(filter.toLowerCase()) ||
                    s.category?.toLowerCase().includes(filter.toLowerCase())
                )
              : skills;

            const categories = [
              ...new Set(filteredSkills.map((s) => s.category ?? "other")),
            ].sort();

            return (
              <div
                key={role}
                className="node-card"
              >
                {/* ── Node Header ──────────────────────────────────── */}
                <button
                  className="node-card__header"
                  onClick={() => toggleNode(role)}
                  type="button"
                >
                  <span
                    className="node-card__dot"
                    style={{
                      background: reachable
                        ? "var(--status-success)"
                        : "var(--status-error)",
                    }}
                  />
                  <div className="node-card__identity">
                    <span className="node-card__name">
                      Node {NODE_INDEX[role]} Hermes Agent
                    </span>
                    <span className="node-card__host">
                      {nodeInfo?.host ?? "unknown"}
                    </span>
                  </div>
                  <div className="node-card__status">
                    {reachable ? (
                      <Wifi size={13} style={{ color: "var(--status-success)" }} />
                    ) : (
                      <WifiOff size={13} style={{ color: "var(--status-error)" }} />
                    )}
                  </div>
                  <span className="node-card__chevron">
                    {isExpanded ? (
                      <ChevronDown size={16} />
                    ) : (
                      <ChevronRight size={16} />
                    )}
                  </span>
                </button>

                {/* ── Expanded Body ────────────────────────────────── */}
                {isExpanded && (
                  <div className="node-card__body">
                    {/* ── Profiles Section ─────────────────────────── */}
                    <div className="node-card__section">
                      <div className="node-card__section-header">
                        <User size={13} />
                        <span>Profiles</span>
                        <span className="node-card__badge">
                          {nodeInfo?.profiles?.length ?? 0}
                        </span>
                      </div>
                      {nodeInfo?.profiles && nodeInfo.profiles.length > 0 ? (
                        <div className="node-card__profile-list">
                          {nodeInfo.profiles.map((p) => (
                            <div key={p.name} className="node-card__profile-row">
                              <div className="node-card__profile-header">
                                <span
                                  className="node-card__profile-dot"
                                  style={{
                                    background: p.gateway_running
                                      ? color
                                      : "var(--text-tertiary)",
                                  }}
                                />
                                <span className="node-card__profile-name">
                                  {p.name}
                                </span>
                                {p.is_default && (
                                  <span className="node-card__profile-default">
                                    active
                                  </span>
                                )}
                                {p.gateway_running && (
                                  <span className="node-card__profile-gateway">
                                    <Zap size={10} />
                                    gateway
                                  </span>
                                )}
                              </div>
                              <div className="node-card__profile-meta">
                                {p.model && (
                                  <span className="node-card__profile-tag">
                                    <Cpu size={10} />
                                    {p.model}
                                  </span>
                                )}
                                {p.provider && (
                                  <span className="node-card__profile-tag">
                                    {p.provider}
                                  </span>
                                )}
                                {typeof p.skill_count === "number" && (
                                  <span className="node-card__profile-tag">
                                    <Sparkles size={10} />
                                    {p.skill_count} skills
                                  </span>
                                )}
                              </div>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <span className="node-card__empty-hint">
                          {reachable
                            ? "No profiles installed"
                            : "Node unreachable"}
                        </span>
                      )}
                    </div>

                    {/* ── Skills Section ───────────────────────────── */}
                    <div className="node-card__section">
                      <button
                        className="node-card__section-header node-card__section-header--toggle"
                        onClick={() => toggleSkillsSection(role)}
                        type="button"
                      >
                        <Sparkles size={13} />
                        <span>Skills</span>
                        <span className="node-card__badge">
                          {skills.length}
                        </span>
                        <span className="node-card__chevron-mini">
                          {isSkillsExpanded ? (
                            <ChevronDown size={13} />
                          ) : (
                            <ChevronRight size={13} />
                          )}
                        </span>
                      </button>

                      {isSkillsExpanded && (
                        <>
                          {isSkillsLoading && <Skeleton variant="list" lines={3} />}

                          {skillsError && (
                            <div className="node-card__error">
                              <span>{skillsError}</span>
                              <ActionButton
                                variant="secondary"
                                onClick={() => fetchSkills(role)}
                                style={{
                                  padding: "2px 8px",
                                  height: 22,
                                  fontSize: "var(--text-xs)",
                                }}
                              >
                                Retry
                              </ActionButton>
                            </div>
                          )}

                          {!isSkillsLoading &&
                            !skillsError &&
                            filteredSkills.length === 0 && (
                              <span className="node-card__empty-hint">
                                {filter
                                  ? "No matching skills"
                                  : "No skills installed"}
                              </span>
                            )}

                          {!isSkillsLoading &&
                            !skillsError &&
                            filteredSkills.length > 0 && (
                              <div className="node-card__skills-list">
                                {categories.map((cat) => {
                                  const catSkills = filteredSkills.filter(
                                    (s) => (s.category ?? "other") === cat
                                  );
                                  if (catSkills.length === 0) return null;
                                  return (
                                    <div key={cat} className="node-card__skill-group">
                                      <div className="node-card__skill-category">
                                        {cat}{" "}
                                        <span className="node-card__skill-count">
                                          ({catSkills.length})
                                        </span>
                                      </div>
                                      {catSkills.map((skill) => (
                                        <div
                                          key={skill.name}
                                          className="node-card__skill-row"
                                          style={{
                                            opacity:
                                              skill.enabled === false ? 0.5 : 1,
                                          }}
                                        >
                                          <div className="node-card__skill-info">
                                            <span className="node-card__skill-name">
                                              {skill.name}
                                            </span>
                                            {skill.description && (
                                              <span className="node-card__skill-desc">
                                                {skill.description}
                                              </span>
                                            )}
                                          </div>
                                          <ActionButton
                                            variant={
                                              skill.enabled !== false
                                                ? "secondary"
                                                : "primary"
                                            }
                                            loading={
                                              togglingSkill ===
                                              `${role}:${skill.name}`
                                            }
                                            onClick={() =>
                                              toggleSkill(role, skill)
                                            }
                                            style={{
                                              padding: "2px 10px",
                                              height: 24,
                                              fontSize: "var(--text-xs)",
                                              flexShrink: 0,
                                            }}
                                          >
                                            {skill.enabled !== false
                                              ? "On"
                                              : "Off"}
                                          </ActionButton>
                                        </div>
                                      ))}
                                    </div>
                                  );
                                })}
                              </div>
                            )}

                          {/* Skills footer */}
                          {!isSkillsLoading && !skillsError && skills.length > 0 && (
                            <div className="node-card__skills-footer">
                              <span>
                                {skills.filter((s) => s.enabled !== false).length}{" "}
                                / {skills.length} enabled
                              </span>
                              <ActionButton
                                variant="secondary"
                                onClick={() => fetchSkills(role)}
                                style={{
                                  padding: "2px 8px",
                                  height: 22,
                                  fontSize: "var(--text-xs)",
                                }}
                              >
                                Refresh
                              </ActionButton>
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </Panel>
  );
}
