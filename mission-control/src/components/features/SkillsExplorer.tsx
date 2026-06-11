"use client";

import { useEffect, useState, useCallback } from "react";
import { Panel } from "@/components/ui/Panel";
import { ActionButton } from "@/components/ui/ActionButton";
import { Skeleton } from "@/components/ui/Skeleton";
import { EmptyState } from "@/components/ui/EmptyState";
import { useToast } from "@/hooks/useToast";
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";
import { Sparkles } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface Skill {
  name: string;
  description?: string;
  category?: string;
  enabled?: boolean;
}

const ROLES: AgentRole[] = ["planner", "executor", "auditor"];
const ROLE_LABELS: Record<AgentRole, string> = { planner: "Planner", executor: "Executor", auditor: "Auditor", critic: "Critic", conflict_resolver: "Conflict Resolver", cleaner: "Cleaner", decider: "Decider" };

// ── Main Component ────────────────────────────────────────────────────

export default function SkillsExplorer() {
  const [activeTab, setActiveTab] = useState<AgentRole>("planner");
  const [skills, setSkills] = useState<Skill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [togglingSkill, setTogglingSkill] = useState<string | null>(null);
  const { toast } = useToast();

  const fetchSkills = useCallback(async (role: AgentRole) => {
    setLoading(true); setError(null); setFilter("");
    try {
      const res = await fetch(`/api/skills?node=${role}`, { cache: "no-store" });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { error?: string };
        setError(body.error ?? `HTTP ${res.status}`); setSkills([]); return;
      }
      const data = (await res.json()) as { skills?: Skill[] };
      setSkills(data.skills ?? []);
    } catch (err) { setError(err instanceof Error ? err.message : "Fetch failed"); setSkills([]); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { const t = setTimeout(() => void fetchSkills(activeTab), 0); return () => clearTimeout(t); }, [activeTab, fetchSkills]);

  const toggleSkill = async (skill: Skill) => {
    setTogglingSkill(skill.name);
    try {
      const res = await fetch("/api/skills", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          node: activeTab,
          name: skill.name,
          enabled: !skill.enabled,
        }),
      });
      if (res.ok) {
        setSkills((prev) =>
          prev.map((s) =>
            s.name === skill.name ? { ...s, enabled: !s.enabled } : s
          )
        );
        toast({ type: "success", message: `Skill '${skill.name}' ${!skill.enabled ? "enabled" : "disabled"}.` });
      } else {
        toast({ type: "error", message: "Failed to toggle skill." });
      }
    } catch { toast({ type: "error", message: "Network error toggling skill." }); }
    finally { setTogglingSkill(null); }
  };

  // Group skills by category
  const filteredSkills = skills.filter((s) =>
    !filter || s.name.toLowerCase().includes(filter.toLowerCase()) ||
    s.description?.toLowerCase().includes(filter.toLowerCase()) ||
    s.category?.toLowerCase().includes(filter.toLowerCase())
  );

  const categories = [...new Set(filteredSkills.map((s) => s.category ?? "other"))].sort();

  return (
    <Panel title="Agent Skills" emptyIcon={Sparkles} emptyMessage="No skills yet">
      <div style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
        {/* Tabs */}
        <div style={{ display: "flex", borderBottom: "1px solid var(--border-default)", flexShrink: 0, marginBottom: "var(--space-3)" }}>
          {ROLES.map((role) => {
            const isActive = activeTab === role;
            return (
              <button key={role} id={`skills-tab-${role}`} onClick={() => setActiveTab(role)}
                style={{
                  flex: 1, padding: "var(--space-2)", fontSize: "var(--text-xs)",
                  fontWeight: "var(--weight-medium)", fontFamily: "var(--font-sans)",
                  textTransform: "uppercase", letterSpacing: "0.05em", cursor: "pointer",
                  background: "transparent", border: "none",
                  borderBottom: isActive ? `2px solid ${AGENT_COLORS[role]}` : "2px solid transparent",
                  color: isActive ? AGENT_COLORS[role] : "var(--text-tertiary)",
                  transition: "color 150ms ease, border-color 150ms ease",
                }}
                onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.color = "var(--text-secondary)"; }}
                onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.color = "var(--text-tertiary)"; }}
              >
                {ROLE_LABELS[role]}
              </button>
            );
          })}
        </div>

        {/* Search filter */}
        {!loading && !error && skills.length > 0 && (
          <div style={{ flexShrink: 0, marginBottom: "var(--space-3)" }}>
            <input
              type="text"
              placeholder="Filter skills..."
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              style={{
                width: "100%", padding: "var(--space-2) var(--space-3)",
                fontSize: "var(--text-sm)", fontFamily: "var(--font-sans)",
                background: "var(--surface-hover)", border: "1px solid var(--border-default)",
                borderRadius: "var(--radius-sm)", color: "var(--text-primary)",
                outline: "none",
              }}
            />
          </div>
        )}

        {/* Body */}
        <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
          {loading && <Skeleton variant="list" lines={4} />}

          {error && (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "var(--space-3)", padding: "var(--space-6)" }}>
              <span style={{ fontSize: "var(--text-sm)", color: "var(--status-error)" }}>Dashboard unreachable</span>
              <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>{error}</span>
              <ActionButton variant="secondary" onClick={() => fetchSkills(activeTab)}>Retry</ActionButton>
            </div>
          )}

          {!loading && !error && skills.length === 0 && (
            <EmptyState icon={Sparkles} message="No skills installed" hint="Install skills via the Hermes CLI: hermes skills install" />
          )}

          {!loading && !error && filteredSkills.length === 0 && skills.length > 0 && (
            <EmptyState icon={Sparkles} message="No matching skills" hint="Try a different search term." />
          )}

          {!loading && !error && filteredSkills.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
              {categories.map((cat) => {
                const catSkills = filteredSkills.filter((s) => (s.category ?? "other") === cat);
                if (catSkills.length === 0) return null;
                return (
                  <div key={cat}>
                    {/* Category header */}
                    <div style={{
                      fontSize: "var(--text-xs)", fontWeight: "var(--weight-semibold)",
                      textTransform: "uppercase", letterSpacing: "0.08em",
                      color: "var(--text-tertiary)", padding: "0 var(--space-3)",
                      marginBottom: "var(--space-1)",
                    }}>
                      {cat} <span style={{ fontWeight: "var(--weight-normal)", opacity: 0.6 }}>({catSkills.length})</span>
                    </div>

                    {/* Skill rows */}
                    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-1)" }}>
                      {catSkills.map((skill) => (
                        <div key={skill.name}
                          style={{
                            display: "flex", alignItems: "center", gap: "var(--space-2)",
                            padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-sm)",
                            background: "var(--surface-hover)", transition: "background 150ms ease",
                            opacity: skill.enabled === false ? 0.5 : 1,
                          }}
                          onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-active)"; }}
                          onMouseLeave={(e) => { e.currentTarget.style.background = "var(--surface-hover)"; }}
                        >
                          {/* Name and description */}
                          <div style={{ flex: 1, minWidth: 0 }}>
                            <span style={{
                              fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)",
                              color: "var(--text-primary)", overflow: "hidden",
                              textOverflow: "ellipsis", whiteSpace: "nowrap", display: "block",
                            }}>{skill.name}</span>
                            {skill.description && (
                              <span style={{
                                fontSize: "var(--text-xs)", color: "var(--text-tertiary)",
                                overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                                display: "block", marginTop: 1,
                              }}>{skill.description}</span>
                            )}
                          </div>

                          {/* Toggle button */}
                          <ActionButton
                            variant={skill.enabled !== false ? "secondary" : "primary"}
                            loading={togglingSkill === skill.name}
                            onClick={() => toggleSkill(skill)}
                            style={{ padding: "2px 10px", height: 24, fontSize: "var(--text-xs)", flexShrink: 0 }}
                          >
                            {skill.enabled !== false ? "On" : "Off"}
                          </ActionButton>
                        </div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* Footer */}
        {!loading && !error && skills.length > 0 && (
          <div style={{
            flexShrink: 0, borderTop: "1px solid var(--border-default)",
            padding: "var(--space-2) var(--space-3)", marginTop: "var(--space-2)",
            display: "flex", justifyContent: "space-between", alignItems: "center",
          }}>
            <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>
              {skills.length} skill{skills.length === 1 ? "" : "s"} · {skills.filter((s) => s.enabled !== false).length} enabled
            </span>
            <ActionButton variant="secondary" onClick={() => fetchSkills(activeTab)}
              style={{ padding: "2px 8px", height: 22, fontSize: "var(--text-xs)" }}
            >
              Refresh
            </ActionButton>
          </div>
        )}
      </div>
    </Panel>
  );
}
