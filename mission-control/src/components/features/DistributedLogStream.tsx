"use client";

/**
 * DistributedLogStream — unified chronological log viewer for multi-agent systems.
 *
 * Design principles:
 *  - ONE stream, all agents interleaved by timestamp (like `kubectl logs --prefix`)
 *  - Dynamically discovers agents from the log data — no hardcoded role names
 *  - Agent filter pills: click to toggle focus per agent (multi-select)
 *  - Level filter: ERR / WRN / INF / DBG toggles
 *  - Search: real-time substring filter on message text
 *  - Smart auto-scroll with "↓ N new" pill when scrolled up
 *  - Each line: timestamp | agent pill | level badge | message
 *  - Color per agent via deterministic HSL hash (handles any profile name)
 *
 * Used in both live (SSE) and completed (archived REST) modes.
 */

import React, {
  useRef, useState, useEffect, useCallback, useMemo, memo,
} from "react";
import { authorColor } from "@/lib/design-tokens";
import { Search, X, ChevronDown } from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────

export interface LogLine {
  id: string;
  agent: string;    // dynamic — any value the daemon emits
  level: string;    // error | warn | warning | info | debug | …
  message: string;
  timestamp: string; // ISO or HH:MM:SS
}

// ── Constants ──────────────────────────────────────────────────────────

const LEVEL_ORDER = ["error", "warn", "warning", "info", "debug"];

const LEVEL_LABEL: Record<string, string> = {
  error:   "ERR",
  warn:    "WRN",
  warning: "WRN",
  info:    "INF",
  debug:   "DBG",
};

const LEVEL_COLOR: Record<string, string> = {
  error:   "var(--status-error)",
  warn:    "hsl(38, 92%, 50%)",
  warning: "hsl(38, 92%, 50%)",
  info:    "var(--status-running)",
  debug:   "var(--text-tertiary)",
};

// ── Helpers ────────────────────────────────────────────────────────────

function fmtTs(ts: string): string {
  if (!ts) return "??:??:??";
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts.slice(0, 8);
    return d.toLocaleTimeString("en-US", { hour12: false, hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return ts.slice(0, 8);
  }
}

function normalizeLevel(level: string): string {
  return (level ?? "info").toLowerCase();
}

/** Short, display-friendly agent name (e.g. "expert.astronomy" → "astronomy") */
function shortAgentName(agent: string): string {
  if (!agent) return "unknown";
  // Strip common prefixes like "expert.", "worker.", "universal-"
  return agent
    .replace(/^(expert|worker|agent|universal)[-._]/, "")
    .replace(/_/g, " ");
}

// ── Agent Pill ─────────────────────────────────────────────────────────

const AgentPill = memo(function AgentPill({
  agent,
  active,
  count,
  onClick,
}: {
  agent: string;
  active: boolean;
  count: number;
  onClick: () => void;
}) {
  const color = authorColor(agent);
  return (
    <button
      className={`dls-agent-pill ${active ? "dls-agent-pill--active" : ""}`}
      style={{
        borderColor: active ? color : "transparent",
        background: active ? `${color}18` : undefined,
      }}
      onClick={onClick}
      title={agent}
    >
      <span className="dls-agent-pill__dot" style={{ background: color }} />
      <span className="dls-agent-pill__name">{shortAgentName(agent)}</span>
      <span className="dls-agent-pill__count">{count}</span>
    </button>
  );
});

// ── Level Badge ────────────────────────────────────────────────────────

const LevelBadge = memo(function LevelBadge({ level }: { level: string }) {
  const norm = normalizeLevel(level);
  const label = LEVEL_LABEL[norm] ?? norm.slice(0, 3).toUpperCase();
  const color = LEVEL_COLOR[norm] ?? "var(--text-tertiary)";
  return (
    <span className="dls-level" style={{ color }}>
      {label}
    </span>
  );
});

// ── Log Row ────────────────────────────────────────────────────────────

const LogRow = memo(function LogRow({
  line,
  search,
}: {
  line: LogLine;
  search: string;
}) {
  const agentColor = authorColor(line.agent);
  const msg = line.message;

  // Highlight search term in message
  let msgNode: React.ReactNode = msg;
  if (search && msg.toLowerCase().includes(search.toLowerCase())) {
    const idx = msg.toLowerCase().indexOf(search.toLowerCase());
    msgNode = (
      <>
        {msg.slice(0, idx)}
        <mark className="dls-highlight">{msg.slice(idx, idx + search.length)}</mark>
        {msg.slice(idx + search.length)}
      </>
    );
  }

  return (
    <div className="dls-row">
      {/* Timestamp */}
      <span className="dls-row__ts">{fmtTs(line.timestamp)}</span>

      {/* Agent dot + name */}
      <span className="dls-row__agent" title={line.agent}>
        <span className="dls-row__agent-dot" style={{ background: agentColor }} />
        <span className="dls-row__agent-name" style={{ color: agentColor }}>
          {shortAgentName(line.agent)}
        </span>
      </span>

      {/* Level */}
      <LevelBadge level={line.level} />

      {/* Message */}
      <span className="dls-row__msg">{msgNode}</span>
    </div>
  );
});

// ── Main Component ─────────────────────────────────────────────────────

interface DistributedLogStreamProps {
  lines: LogLine[];
  isLive?: boolean;
  /** Optional label shown in the header stats area */
  sourceLabel?: string;
}

export function DistributedLogStream({
  lines,
  isLive = false,
  sourceLabel,
}: DistributedLogStreamProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isAtBottom, setIsAtBottom] = useState(true);
  const [newCount, setNewCount] = useState(0);
  const prevLen = useRef(lines.length);

  // ── Filter state ────────────────────────────────────────────────────
  const [activeAgents, setActiveAgents] = useState<Set<string>>(new Set()); // empty = all
  const [activeLevels, setActiveLevels] = useState<Set<string>>(new Set()); // empty = all
  const [search, setSearch] = useState("");

  // ── Discover agents dynamically ────────────────────────────────────
  const agentCounts = useMemo(() => {
    const map = new Map<string, number>();
    for (const line of lines) {
      if (line.agent) {
        map.set(line.agent, (map.get(line.agent) ?? 0) + 1);
      }
    }
    // Sort: known order first, then alphabetical
    return new Map(
      [...map.entries()].sort(([a], [b]) => {
        const knownOrder = ["planner", "executor", "auditor", "critic", "conflict_resolver", "cleaner", "decider"];
        const ai = knownOrder.indexOf(a);
        const bi = knownOrder.indexOf(b);
        if (ai >= 0 && bi >= 0) return ai - bi;
        if (ai >= 0) return -1;
        if (bi >= 0) return 1;
        return a.localeCompare(b);
      })
    );
  }, [lines]);

  // Discover which levels appear
  const levelCounts = useMemo(() => {
    const map = new Map<string, number>();
    for (const line of lines) {
      const norm = normalizeLevel(line.level);
      const canon = norm === "warning" ? "warn" : norm;
      map.set(canon, (map.get(canon) ?? 0) + 1);
    }
    return map;
  }, [lines]);

  // ── Filtered lines ─────────────────────────────────────────────────
  const filtered = useMemo(() => {
    let result = lines;
    if (activeAgents.size > 0) {
      result = result.filter((l) => activeAgents.has(l.agent));
    }
    if (activeLevels.size > 0) {
      result = result.filter((l) => {
        const norm = normalizeLevel(l.level);
        const canon = norm === "warning" ? "warn" : norm;
        return activeLevels.has(canon);
      });
    }
    if (search.trim()) {
      const q = search.trim().toLowerCase();
      result = result.filter((l) =>
        l.message.toLowerCase().includes(q) ||
        l.agent.toLowerCase().includes(q)
      );
    }
    return result;
  }, [lines, activeAgents, activeLevels, search]);

  // ── Auto-scroll ────────────────────────────────────────────────────
  useEffect(() => {
    const added = filtered.length - prevLen.current;
    prevLen.current = filtered.length;
    if (added > 0) {
      if (isAtBottom && scrollRef.current) {
        scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      } else if (!isAtBottom) {
        setNewCount((p) => p + added);
      }
    }
  }, [filtered.length, isAtBottom]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 60;
    setIsAtBottom(near);
    if (near) setNewCount(0);
  }, []);

  const snapToBottom = useCallback(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    setIsAtBottom(true);
    setNewCount(0);
  }, []);

  // ── Toggle helpers ─────────────────────────────────────────────────
  const toggleAgent = useCallback((agent: string) => {
    setActiveAgents((prev) => {
      const next = new Set(prev);
      if (next.has(agent)) next.delete(agent);
      else next.add(agent);
      return next;
    });
  }, []);

  const toggleLevel = useCallback((level: string) => {
    setActiveLevels((prev) => {
      const next = new Set(prev);
      if (next.has(level)) next.delete(level);
      else next.add(level);
      return next;
    });
  }, []);

  const clearFilters = useCallback(() => {
    setActiveAgents(new Set());
    setActiveLevels(new Set());
    setSearch("");
  }, []);

  const hasFilters = activeAgents.size > 0 || activeLevels.size > 0 || search.trim().length > 0;

  // ── Render ─────────────────────────────────────────────────────────
  return (
    <div className="dls">
      {/* ── Toolbar ─────────────────────────────────────────────────── */}
      <div className="dls-toolbar">
        {/* Search */}
        <div className="dls-search">
          <Search size={12} className="dls-search__icon" />
          <input
            className="dls-search__input"
            placeholder="Filter logs…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            spellCheck={false}
          />
          {search && (
            <button className="dls-search__clear" onClick={() => setSearch("")} aria-label="Clear search">
              <X size={10} />
            </button>
          )}
        </div>

        {/* Level toggles */}
        <div className="dls-level-filters">
          {[...levelCounts.entries()]
            .sort(([a], [b]) => LEVEL_ORDER.indexOf(a) - LEVEL_ORDER.indexOf(b))
            .map(([level, count]) => {
              const active = activeLevels.has(level) || activeLevels.size === 0;
              const color = LEVEL_COLOR[level] ?? "var(--text-tertiary)";
              return (
                <button
                  key={level}
                  className={`dls-level-btn ${activeLevels.size > 0 && !activeLevels.has(level) ? "dls-level-btn--muted" : ""}`}
                  style={{ color: active ? color : undefined }}
                  onClick={() => toggleLevel(level)}
                  title={`Toggle ${level} logs`}
                >
                  {LEVEL_LABEL[level] ?? level.toUpperCase().slice(0, 3)}
                  <span className="dls-level-btn__count">{count}</span>
                </button>
              );
            })}
        </div>

        {/* Clear filters */}
        {hasFilters && (
          <button className="dls-clear-btn" onClick={clearFilters} title="Clear all filters">
            <X size={11} />
            Clear
          </button>
        )}

        {/* Stats */}
        <span className="dls-stats">
          {filtered.length !== lines.length
            ? `${filtered.length} / ${lines.length}`
            : lines.length}{" "}
          lines
          {sourceLabel && <span className="dls-stats__source"> · {sourceLabel}</span>}
          {isLive && (
            <span className="dls-live-dot" title="Live streaming" />
          )}
        </span>
      </div>

      {/* ── Agent pills ─────────────────────────────────────────────── */}
      {agentCounts.size > 0 && (
        <div className="dls-agents">
          {[...agentCounts.entries()].map(([agent, count]) => (
            <AgentPill
              key={agent}
              agent={agent}
              active={activeAgents.size === 0 || activeAgents.has(agent)}
              count={count}
              onClick={() => toggleAgent(agent)}
            />
          ))}
        </div>
      )}

      {/* ── Log stream ──────────────────────────────────────────────── */}
      <div className="dls-stream-wrapper">
        {filtered.length === 0 ? (
          <div className="dls-empty">
            {hasFilters ? "No logs match the current filters." : "No log output yet."}
          </div>
        ) : (
          <div
            ref={scrollRef}
            className="dls-stream"
            onScroll={handleScroll}
          >
            {filtered.map((line) => (
              <LogRow key={line.id} line={line} search={search} />
            ))}
          </div>
        )}

        {/* Jump-to-bottom pill */}
        {newCount > 0 && (
          <button className="new-output-pill" onClick={snapToBottom}>
            <ChevronDown size={11} />
            {newCount} new line{newCount !== 1 ? "s" : ""}
          </button>
        )}
      </div>
    </div>
  );
}

export default DistributedLogStream;
