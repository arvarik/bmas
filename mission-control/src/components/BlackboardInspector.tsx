"use client";

import { useEffect, useState, useCallback } from "react";
import { useBlackboard, type DaemonState } from "@/hooks/useBlackboard";
import { Panel } from "@/components/ui/Panel";
import { SplitView } from "@/components/ui/SplitView";
import { Skeleton } from "@/components/ui/Skeleton";
import { Clipboard } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface PrivateEntry {
  key: string;
  type: string;
  value: string | string[] | Record<string, string> | null;
}

interface PrivateResponse {
  entries: PrivateEntry[];
  error?: string;
}

const PRIVATE_POLL_INTERVAL = 4_000;

// ── Helpers ───────────────────────────────────────────────────────────

function renderValue(value: string | string[] | Record<string, string> | null): string {
  if (value === null) return "—";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.join(", ");
  return JSON.stringify(value, null, 2);
}

function typeBadgeStyle(type: string): React.CSSProperties {
  const colors: Record<string, { bg: string; fg: string }> = {
    string: { bg: "hsl(217,91%,60%,0.15)", fg: "var(--status-running)" },
    hash: { bg: "hsl(265,50%,60%,0.15)", fg: "var(--agent-planner)" },
    list: { bg: "hsl(38,92%,50%,0.15)", fg: "var(--status-paused)" },
    set: { bg: "hsl(142,71%,45%,0.15)", fg: "var(--status-success)" },
    zset: { bg: "hsl(0,84%,60%,0.15)", fg: "var(--status-error)" },
  };
  const c = colors[type] ?? { bg: "var(--surface-hover)", fg: "var(--text-tertiary)" };
  return {
    fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)",
    textTransform: "uppercase" as const, padding: "1px 6px",
    borderRadius: "var(--radius-sm)", background: c.bg, color: c.fg,
  };
}

// ── Public State Pane ─────────────────────────────────────────────────

function PublicPane({ state }: { state: DaemonState | null }) {
  if (!state) return <Skeleton variant="list" lines={4} />;

  const agents = state.agents
    ? (Object.entries(state.agents) as [string, typeof state.agents.planner][])
    : [];
  const taskCount = state.tasks ? Object.keys(state.tasks).length : 0;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
      {/* Top-level fields */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "var(--space-2)" }}>
        <StateField label="Phase" value={state.phase ?? "—"} />
        <StateField label="Iteration" value={String(state.iteration ?? 0)} />
        <StateField label="Paused" value={state.paused ? "YES" : "no"} highlight={state.paused} />
        <StateField label="Tasks" value={String(taskCount)} />
      </div>

      {/* Agent heartbeats */}
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
        <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)", color: "var(--text-tertiary)", textTransform: "uppercase", letterSpacing: "0.1em" }}>
          Agent Status
        </span>
        {agents.map(([name, agent]) => (
          <div key={name} style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-sm)", background: "var(--surface-hover)" }}>
            <span style={{ width: 8, height: 8, borderRadius: "var(--radius-full)", background: agent.alive ? "var(--status-success)" : "var(--status-error)", flexShrink: 0 }} />
            <span style={{ fontSize: "var(--text-sm)", fontWeight: "var(--weight-medium)", color: "var(--text-primary)", textTransform: "capitalize" }}>{name}</span>
            <span style={{ marginLeft: "auto", fontSize: "var(--text-xs)", color: "var(--text-tertiary)", fontFamily: "var(--font-mono)" }}>
              {agent.last_heartbeat ? new Date(agent.last_heartbeat).toLocaleTimeString() : "—"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function StateField({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-sm)", background: "var(--surface-hover)" }}>
      <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--text-tertiary)" }}>{label}</span>
      <span style={{ fontSize: "var(--text-sm)", fontFamily: "var(--font-mono)", color: highlight ? "var(--status-paused)" : "var(--text-primary)", fontWeight: highlight ? "var(--weight-semibold)" : "var(--weight-regular)" }}>{value}</span>
    </div>
  );
}

// ── Private State Pane ────────────────────────────────────────────────

function PrivatePane() {
  const [entries, setEntries] = useState<PrivateEntry[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const fetchPrivate = useCallback(async () => {
    try {
      const res = await fetch("/api/private", { cache: "no-store" });
      if (!res.ok) { setError(`HTTP ${res.status}`); return; }
      const data = (await res.json()) as PrivateResponse;
      if (data.error) { setError(data.error); } else { setEntries(data.entries); setError(null); }
    } catch (err) { setError(err instanceof Error ? err.message : "Fetch failed"); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void fetchPrivate(), 0);
    const id = setInterval(fetchPrivate, PRIVATE_POLL_INTERVAL);
    return () => { clearTimeout(timer); clearInterval(id); };
  }, [fetchPrivate]);

  if (loading) return <Skeleton variant="list" lines={4} />;
  if (error) return (
    <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "var(--space-6)", gap: "var(--space-2)" }}>
      <span style={{ fontSize: "var(--text-sm)", color: "var(--status-error)" }}>⚠ {error}</span>
      <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)" }}>Retrying every {PRIVATE_POLL_INTERVAL / 1000}s</span>
    </div>
  );
  if (entries.length === 0) return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "var(--space-6)", color: "var(--text-tertiary)", fontSize: "var(--text-sm)" }}>
      No session data
    </div>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
      {entries.map((entry) => (
        <details key={entry.key} style={{ borderRadius: "var(--radius-sm)", background: "var(--surface-hover)", overflow: "hidden" }}>
          <summary style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", padding: "var(--space-2) var(--space-3)", cursor: "pointer", fontSize: "var(--text-sm)" }}>
            <span style={typeBadgeStyle(entry.type)}>{entry.type}</span>
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-mono)", color: "var(--text-primary)" }}>{entry.key.replace("bmas:private:", "")}</span>
          </summary>
          <div style={{ padding: "var(--space-2) var(--space-3)", borderTop: "1px solid var(--border-default)", background: "var(--surface-base)" }}>
            <pre style={{ fontFamily: "var(--font-mono)", fontSize: "var(--text-mono)", lineHeight: "var(--leading-mono)", color: "var(--text-secondary)", whiteSpace: "pre-wrap", wordBreak: "break-all", maxHeight: 128, overflow: "auto", margin: 0 }}>
              {renderValue(entry.value)}
            </pre>
          </div>
        </details>
      ))}
    </div>
  );
}

// ── Main Component ────────────────────────────────────────────────────

export default function BlackboardInspector() {
  const daemonState = useBlackboard((s) => s.state);
  const loading = useBlackboard((s) => s.loading);
  const error = useBlackboard((s) => s.error);

  const panelStatus = loading ? "loading" as const : error ? "error" as const : undefined;

  return (
    <Panel
      title="Blackboard"
      subtitle="State inspector"
      status={panelStatus}
      errorMessage={error ? "Redis connection lost" : undefined}
      emptyIcon={Clipboard}
      emptyMessage="No session data"
      emptyHint="Start a swarm session to populate the blackboard."
    >
      <SplitView
        leftHeader="Public Consensus"
        rightHeader="Private Debate"
        left={<PublicPane state={daemonState} />}
        right={<PrivatePane />}
      />
    </Panel>
  );
}
