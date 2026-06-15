"use client";

/**
 * DistributedLogStream — unified, chronological log viewer for the bMAS
 * distributed agent swarm.
 *
 * Design principles:
 *  - ONE stream, all agents interleaved by timestamp (like `kubectl logs --prefix`)
 *  - Logs are collected from every node — daemon, control unit, and each
 *    agent/persona — and attributed to its source (no daemon-only view).
 *  - Dynamically discovers agents from the log data — no hardcoded role names.
 *  - Agent filter pills: click to toggle focus per agent (multi-select).
 *  - Level filter: ERROR / WARNING / INFO / DEBUG toggles (full words, never
 *    abbreviated).
 *  - Search: real-time substring filter on message text + agent name.
 *  - Smart auto-scroll with "↓ N new" pill when scrolled up.
 *  - Each row is CLICKABLE: opens a detail drawer with the FULL structured
 *    record (timestamp, level, agent/persona, node, turn id, complete message,
 *    and every structured field/payload — reasoning, tool calls, usage/cost,
 *    routing rationale, board reads/writes, stack traces, …). Nothing is
 *    truncated.
 *  - Color per agent via deterministic HSL hash (handles any profile name).
 *
 * Used in both live (SSE) and completed (archived REST) modes.
 */

import React, {
  useRef, useState, useEffect, useCallback, useMemo, memo,
} from "react";
import { authorColor } from "@/lib/design-tokens";
import { RichContent } from "@/components/ui/RichContent";
import { Search, X, ChevronDown, Copy, Check, Layers } from "lucide-react";

// ── Types ──────────────────────────────────────────────────────────────

export interface LogLine {
  id: string;
  agent: string;    // dynamic — any value the daemon/agent emits
  level: string;    // error | warning | info | debug | …
  message: string;
  timestamp: string; // ISO or HH:MM:SS
  node?: string;     // originating node endpoint/id
  turnId?: string;   // correlating turn id
  fields?: Record<string, unknown> | null; // structured payload
}

// ── Level model (canonical, full-word labels) ───────────────────────────

const LEVEL_ORDER = ["error", "warning", "info", "debug"];

/** Full, unabbreviated level labels — INFO not INF, WARNING not WRN, etc. */
const LEVEL_LABEL: Record<string, string> = {
  error:   "ERROR",
  warning: "WARNING",
  info:    "INFO",
  debug:   "DEBUG",
};

const LEVEL_COLOR: Record<string, string> = {
  error:   "var(--status-error)",
  warning: "hsl(38, 92%, 55%)",
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

function fmtTsFull(ts: string): string {
  if (!ts) return "—";
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts;
    return d.toLocaleString("en-US", { hour12: false }) + `.${String(d.getMilliseconds()).padStart(3, "0")}`;
  } catch {
    return ts;
  }
}

/** Map any spelling/abbreviation to a canonical level word. */
function normalizeLevel(level: string): string {
  const l = (level ?? "info").toLowerCase().trim();
  if (l === "warn" || l === "wrn") return "warning";
  if (l === "inf") return "info";
  if (l === "err" || l === "fatal" || l === "critical" || l === "crit") return "error";
  if (l === "dbg" || l === "trace") return "debug";
  return l;
}

/** Short, display-friendly agent name (e.g. "expert.astronomy" → "astronomy"). */
function shortAgentName(agent: string): string {
  if (!agent) return "unknown";
  return agent
    .replace(/^(expert|worker|agent|universal)[-._]/, "")
    .replace(/_/g, " ");
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Keys that should be rendered as large, multi-line text blocks. */
const TEXT_BLOCK_KEYS = new Set([
  "output", "text", "reasoning", "persona_preview", "rationale",
  "body", "stderr", "error", "result", "summary",
]);

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
  const label = LEVEL_LABEL[norm] ?? norm.toUpperCase();
  const color = LEVEL_COLOR[norm] ?? "var(--text-tertiary)";
  return (
    <span className="dls-level" style={{ color }}>
      {label}
    </span>
  );
});

// ── Structured-field renderer (detail view) ──────────────────────────────

function FieldValue({ value }: { value: unknown }): React.ReactElement {
  if (value === null || value === undefined) {
    return <span className="dls-field__null">—</span>;
  }
  if (typeof value === "boolean") {
    return <span className="dls-field__bool">{value ? "true" : "false"}</span>;
  }
  if (typeof value === "number") {
    return <span className="dls-field__num">{value}</span>;
  }
  if (typeof value === "string") {
    return <span className="dls-field__str">{value}</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className="dls-field__null">[]</span>;
    const allScalar = value.every((v) => typeof v !== "object" || v === null);
    if (allScalar) {
      return (
        <div className="dls-field__chips">
          {value.map((v, i) => (
            <span key={i} className="dls-field__chip">{String(v)}</span>
          ))}
        </div>
      );
    }
    return (
      <div className="dls-field__list">
        {value.map((v, i) => (
          <div key={i} className="dls-field__list-item">
            <FieldValue value={v} />
          </div>
        ))}
      </div>
    );
  }
  if (isPlainObject(value)) {
    return <FieldTree fields={value} />;
  }
  return <span className="dls-field__str">{String(value)}</span>;
}

function FieldTree({ fields }: { fields: Record<string, unknown> }): React.ReactElement {
  const entries = Object.entries(fields).filter(([, v]) => {
    if (v === undefined || v === null) return false;
    if (v === "") return false;
    if (v === 0) return false;
    if (Array.isArray(v) && v.length === 0) return false;
    return true;
  });
  if (entries.length === 0) {
    return <div className="dls-detail__empty">No structured fields.</div>;
  }
  return (
    <div className="dls-field-tree">
      {entries.map(([key, value]) => {
        const isBlock =
          TEXT_BLOCK_KEYS.has(key) ||
          (typeof value === "string" && value.length > 120);
        return (
          <div key={key} className={`dls-field-row ${isBlock ? "dls-field-row--block" : ""}`}>
            <span className="dls-field__key">{key}</span>
            {isBlock && typeof value === "string" ? (
              <pre className="dls-field__block">{value}</pre>
            ) : (
              <div className="dls-field__val"><FieldValue value={value} /></div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Detail Drawer ────────────────────────────────────────────────────────

function LogDetail({ line, onClose }: { line: LogLine; onClose: () => void }) {
  const [copied, setCopied] = useState(false);
  const norm = normalizeLevel(line.level);
  const agentColor = authorColor(line.agent);

  const copyJson = useCallback(() => {
    const record = {
      timestamp: line.timestamp,
      level: norm,
      agent: line.agent,
      node: line.node,
      turn_id: line.turnId,
      message: line.message,
      fields: line.fields ?? undefined,
    };
    void navigator.clipboard?.writeText(JSON.stringify(record, null, 2));
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }, [line, norm]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const hasFields = isPlainObject(line.fields) && Object.keys(line.fields).length > 0;

  return (
    <div className="dls-detail">
      <div className="dls-detail__header">
        <span className="dls-detail__level" style={{ color: LEVEL_COLOR[norm] }}>
          {LEVEL_LABEL[norm] ?? norm.toUpperCase()}
        </span>
        <span className="dls-detail__agent" style={{ color: agentColor }}>
          <span className="dls-row__agent-dot" style={{ background: agentColor }} />
          {line.agent}
        </span>
        <div className="dls-detail__actions">
          <button className="dls-detail__btn" onClick={copyJson} title="Copy record as JSON">
            {copied ? <Check size={12} /> : <Copy size={12} />}
            {copied ? "Copied" : "Copy"}
          </button>
          <button className="dls-detail__btn dls-detail__close" onClick={onClose} aria-label="Close detail">
            <X size={14} />
          </button>
        </div>
      </div>

      <div className="dls-detail__body">
        <dl className="dls-detail__meta">
          <div><dt>Time</dt><dd>{fmtTsFull(line.timestamp)}</dd></div>
          {line.node && <div><dt>Node</dt><dd>{line.node}</dd></div>}
          {line.turnId && <div><dt>Turn</dt><dd>{line.turnId}</dd></div>}
        </dl>

        <div className="dls-detail__section">
          <div className="dls-detail__section-title">Message</div>
          <RichContent
            content={line.message}
            className="dls-detail__message-rich"
            maxHeight="400px"
          />
        </div>

        <div className="dls-detail__section">
          <div className="dls-detail__section-title">Structured fields</div>
          {hasFields ? (
            <FieldTree fields={line.fields as Record<string, unknown>} />
          ) : (
            <div className="dls-detail__empty">No structured fields for this entry.</div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Log Row ────────────────────────────────────────────────────────────

const LogRow = memo(function LogRow({
  line,
  search,
  selected,
  onSelect,
}: {
  line: LogLine;
  search: string;
  selected: boolean;
  onSelect: (line: LogLine) => void;
}) {
  const agentColor = authorColor(line.agent);
  const msg = line.message;
  const hasFields = isPlainObject(line.fields) && Object.keys(line.fields).length > 0;

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
    <div
      className={`dls-row ${selected ? "dls-row--selected" : ""}`}
      role="button"
      tabIndex={0}
      onClick={() => onSelect(line)}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSelect(line);
        }
      }}
    >
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

      {/* Message (full — wraps, never truncated) */}
      <span className="dls-row__msg">{msgNode}</span>

      {/* Structured-fields affordance */}
      {hasFields && (
        <span className="dls-row__fields-icon" title="Has structured detail">
          <Layers size={12} />
        </span>
      )}
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

  // ── Filter + selection state ────────────────────────────────────────
  const [activeAgents, setActiveAgents] = useState<Set<string>>(new Set()); // empty = all
  const [activeLevels, setActiveLevels] = useState<Set<string>>(new Set()); // empty = all
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

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
        const knownOrder = ["daemon", "control_unit", "planner", "executor", "auditor", "critic", "conflict_resolver", "cleaner", "decider"];
        const ai = knownOrder.indexOf(a);
        const bi = knownOrder.indexOf(b);
        if (ai >= 0 && bi >= 0) return ai - bi;
        if (ai >= 0) return -1;
        if (bi >= 0) return 1;
        return a.localeCompare(b);
      })
    );
  }, [lines]);

  // Discover which levels appear (canonical)
  const levelCounts = useMemo(() => {
    const map = new Map<string, number>();
    for (const line of lines) {
      const norm = normalizeLevel(line.level);
      map.set(norm, (map.get(norm) ?? 0) + 1);
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
      result = result.filter((l) => activeLevels.has(normalizeLevel(l.level)));
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

  const selectedLine = useMemo(
    () => (selectedId ? lines.find((l) => l.id === selectedId) ?? null : null),
    [selectedId, lines],
  );

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
    <div className={`dls ${selectedLine ? "dls--detail-open" : ""}`}>
      <div className="dls-main">
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
                    {LEVEL_LABEL[level] ?? level.toUpperCase()}
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
                <LogRow
                  key={line.id}
                  line={line}
                  search={search}
                  selected={line.id === selectedId}
                  onSelect={(l) => setSelectedId(l.id)}
                />
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

      {/* ── Detail drawer ─────────────────────────────────────────────── */}
      {selectedLine && (
        <LogDetail line={selectedLine} onClose={() => setSelectedId(null)} />
      )}
    </div>
  );
}

export default DistributedLogStream;
