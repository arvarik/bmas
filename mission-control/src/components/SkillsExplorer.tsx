"use client";

import { useEffect, useState, useCallback } from "react";
import { Panel } from "@/components/ui/Panel";
import { ActionButton } from "@/components/ui/ActionButton";
import { Skeleton } from "@/components/ui/Skeleton";
import { EmptyState } from "@/components/ui/EmptyState";
import { useToast } from "@/hooks/useToast";
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";
import { Sparkles, Trash2 } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface SkillFile { name: string; size?: number; modified?: string; }

const ROLES: AgentRole[] = ["planner", "executor", "auditor"];
const ROLE_LABELS: Record<AgentRole, string> = { planner: "Planner", executor: "Executor", auditor: "Auditor" };

// ── Main Component ────────────────────────────────────────────────────

export default function SkillsExplorer() {
  const [activeTab, setActiveTab] = useState<AgentRole>("planner");
  const [skills, setSkills] = useState<SkillFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [viewingSkill, setViewingSkill] = useState<string | null>(null);
  const [skillContent, setSkillContent] = useState<string | null>(null);
  const [contentLoading, setContentLoading] = useState(false);
  const [deletingSkill, setDeletingSkill] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<string | null>(null);
  const { toast } = useToast();

  const fetchSkills = useCallback(async (role: AgentRole) => {
    setLoading(true); setError(null); setViewingSkill(null); setSkillContent(null);
    try {
      const res = await fetch(`/api/skills?node=${role}`, { cache: "no-store" });
      if (!res.ok) {
        const body = (await res.json().catch(() => ({}))) as { error?: string };
        setError(body.error ?? `HTTP ${res.status}`); setSkills([]); return;
      }
      const data = (await res.json()) as { skills?: SkillFile[] };
      setSkills(data.skills ?? []);
    } catch (err) { setError(err instanceof Error ? err.message : "Fetch failed"); setSkills([]); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { const t = setTimeout(() => void fetchSkills(activeTab), 0); return () => clearTimeout(t); }, [activeTab, fetchSkills]);

  const viewSkill = async (name: string) => {
    if (viewingSkill === name) { setViewingSkill(null); setSkillContent(null); return; }
    setViewingSkill(name); setContentLoading(true);
    try {
      const res = await fetch(`/api/skills?node=${activeTab}&skill=${encodeURIComponent(name)}`, { cache: "no-store" });
      if (res.ok) { const data = (await res.json()) as { content?: string }; setSkillContent(data.content ?? "No content"); }
      else { setSkillContent("Failed to load"); }
    } catch { setSkillContent("Network error"); }
    finally { setContentLoading(false); }
  };

  const deleteSkill = async (name: string) => {
    if (confirmDelete !== name) { setConfirmDelete(name); return; }
    setDeletingSkill(name); setConfirmDelete(null);
    try {
      const res = await fetch(`/api/skills?node=${activeTab}&skill=${encodeURIComponent(name)}`, { method: "DELETE" });
      if (res.ok) {
        setSkills((prev) => prev.filter((s) => s.name !== name));
        if (viewingSkill === name) { setViewingSkill(null); setSkillContent(null); }
        toast({ type: "success", message: `Skill '${name}' deleted.` });
      } else {
        toast({ type: "error", message: "Failed to delete skill." });
      }
    } catch { toast({ type: "error", message: "Network error deleting skill." }); }
    finally { setDeletingSkill(null); }
  };

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

        {/* Body */}
        <div style={{ flex: 1, minHeight: 0, overflow: "auto" }}>
          {loading && <Skeleton variant="list" lines={4} />}

          {error && (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: "var(--space-3)", padding: "var(--space-6)" }}>
              <span style={{ fontSize: "var(--text-sm)", color: "var(--status-error)" }}>Agent unreachable</span>
              <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>{error}</span>
              <ActionButton variant="secondary" onClick={() => fetchSkills(activeTab)}>Retry</ActionButton>
            </div>
          )}

          {!loading && !error && skills.length === 0 && (
            <EmptyState icon={Sparkles} message="No skills learned yet" hint="Skills emerge as agents complete tasks." />
          )}

          {!loading && !error && skills.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
              {skills.map((skill) => (
                <div key={skill.name}>
                  <div
                    style={{
                      display: "flex", alignItems: "center", gap: "var(--space-2)",
                      padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-sm)",
                      background: "var(--surface-hover)", transition: "background 150ms ease",
                    }}
                    onMouseEnter={(e) => { e.currentTarget.style.background = "var(--surface-active)"; }}
                    onMouseLeave={(e) => { e.currentTarget.style.background = "var(--surface-hover)"; }}
                  >
                    <span style={{ fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", color: "var(--text-primary)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{skill.name}</span>
                    {skill.size !== undefined && (
                      <span style={{ fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", color: "var(--text-tertiary)" }}>{formatBytes(skill.size)}</span>
                    )}
                    <ActionButton variant="secondary" onClick={() => viewSkill(skill.name)} style={{ padding: "2px 8px", height: 24, fontSize: "var(--text-xs)" }}>
                      {viewingSkill === skill.name ? "Hide" : "View"}
                    </ActionButton>
                    <ActionButton variant="danger" loading={deletingSkill === skill.name} onClick={() => deleteSkill(skill.name)} style={{ padding: "2px 8px", height: 24, fontSize: "var(--text-xs)" }}>
                      {confirmDelete === skill.name ? "Confirm?" : <Trash2 size={12} />}
                    </ActionButton>
                  </div>

                  {viewingSkill === skill.name && (
                    <div style={{ margin: "var(--space-1) 0 0 var(--space-1)", borderRadius: "var(--radius-sm)", border: "1px solid var(--border-default)", background: "var(--surface-base)", overflow: "hidden" }}>
                      {contentLoading ? (
                        <div style={{ padding: "var(--space-3)" }}><Skeleton variant="text" lines={3} /></div>
                      ) : (
                        <pre style={{ padding: "var(--space-3)", fontFamily: "var(--font-mono)", fontSize: "var(--text-mono)", lineHeight: "var(--leading-mono)", color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 192, overflow: "auto", margin: 0 }}>
                          {skillContent}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </Panel>
  );
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}K`;
  return `${(bytes / (1024 * 1024)).toFixed(1)}M`;
}
