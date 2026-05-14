"use client";

import { useState } from "react";
import { useBlackboard, type DaemonState } from "@/hooks/useBlackboard";
import { Panel } from "@/components/ui/Panel";
import { Skeleton } from "@/components/ui/Skeleton";
import { Clipboard } from "lucide-react";
import { useEffect, useCallback } from "react";

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

// ── State Field ───────────────────────────────────────────────────────

function StateField({ label, value, highlight }: { label: string; value: string; highlight?: boolean }) {
  return (
    <div className="blackboard-field">
      <span className="blackboard-field__label">{label}</span>
      <span
        className="blackboard-field__value"
        style={{
          color: highlight ? "var(--status-paused)" : "var(--text-primary)",
          fontWeight: highlight ? "var(--weight-semibold)" : "var(--weight-regular)",
        }}
      >
        {value}
      </span>
    </div>
  );
}

// ── Public Pane ───────────────────────────────────────────────────────

function PublicPane({ state }: { state: DaemonState | null }) {
  if (!state) return <Skeleton variant="list" lines={4} />;

  const agents = state.agents
    ? (Object.entries(state.agents) as [string, typeof state.agents.planner][])
    : [];
  const taskCount = state.tasks ? Object.keys(state.tasks).length : 0;

  return (
    <div className="blackboard-pane-content">
      <div className="blackboard-fields-grid">
        <StateField label="Phase" value={state.phase ?? "—"} />
        <StateField label="Iteration" value={String(state.iteration ?? 0)} />
        <StateField label="Paused" value={state.paused ? "YES" : "no"} highlight={state.paused} />
        <StateField label="Tasks" value={String(taskCount)} />
      </div>

      <div className="blackboard-agents-section">
        <span className="blackboard-section-label">Agent Status</span>
        {agents.map(([name, agent]) => (
          <div key={name} className="blackboard-agent-row">
            <span
              className="blackboard-agent-dot"
              style={{ background: agent.alive ? "var(--status-success)" : "var(--status-error)" }}
            />
            <span className="blackboard-agent-name">{name}</span>
            <span className="blackboard-agent-heartbeat">
              {agent.last_heartbeat ? new Date(agent.last_heartbeat).toLocaleTimeString() : "—"}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Private Pane ──────────────────────────────────────────────────────

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
    <div className="blackboard-pane-error">
      <span>⚠ {error}</span>
      <span className="blackboard-pane-error__hint">Retrying every {PRIVATE_POLL_INTERVAL / 1000}s</span>
    </div>
  );
  if (entries.length === 0) return (
    <div className="blackboard-pane-empty">No private session data</div>
  );

  return (
    <div className="blackboard-pane-content">
      {entries.map((entry) => (
        <details key={entry.key} className="blackboard-entry">
          <summary className="blackboard-entry__summary">
            <span style={typeBadgeStyle(entry.type)}>{entry.type}</span>
            <span className="blackboard-entry__key">{entry.key.replace("bmas:private:", "")}</span>
          </summary>
          <div className="blackboard-entry__body">
            <pre className="blackboard-entry__pre">
              {renderValue(entry.value)}
            </pre>
          </div>
        </details>
      ))}
    </div>
  );
}

// ── Main View ─────────────────────────────────────────────────────────

export default function BlackboardView() {
  const daemonState = useBlackboard((s) => s.state);
  const loading = useBlackboard((s) => s.loading);
  const error = useBlackboard((s) => s.error);
  const [activeTab, setActiveTab] = useState<"public" | "private">("public");

  const panelStatus = loading ? "loading" as const : error ? "error" as const : undefined;

  return (
    <div className="view-container blackboard-view">
      <Panel
        title="Blackboard Inspector"
        subtitle="Public consensus and private debate state"
        status={panelStatus}
        errorMessage={error ? "Redis connection lost" : undefined}
        emptyIcon={Clipboard}
        emptyMessage="No session data"
        emptyHint="Start a swarm session to populate the blackboard."
      >
        {/* Mobile tabs */}
        <div className="blackboard-tabs">
          <button
            className={`blackboard-tab ${activeTab === "public" ? "blackboard-tab--active" : ""}`}
            onClick={() => setActiveTab("public")}
          >
            Public Consensus
          </button>
          <button
            className={`blackboard-tab ${activeTab === "private" ? "blackboard-tab--active" : ""}`}
            onClick={() => setActiveTab("private")}
          >
            Private Debate
          </button>
        </div>

        {/* Mobile: show active tab only */}
        <div className="blackboard-mobile-pane">
          {activeTab === "public" ? <PublicPane state={daemonState} /> : <PrivatePane />}
        </div>

        {/* Desktop: split view */}
        <div className="blackboard-split">
          <div className="blackboard-split__pane">
            <h4 className="blackboard-split__header">Public Consensus</h4>
            <PublicPane state={daemonState} />
          </div>
          <div className="blackboard-split__divider" />
          <div className="blackboard-split__pane">
            <h4 className="blackboard-split__header">Private Debate</h4>
            <PrivatePane />
          </div>
        </div>
      </Panel>
    </div>
  );
}
