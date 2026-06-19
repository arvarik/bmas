"use client";

/**
 * BlackboardBoard — the reimagined Blackboard tab.
 *
 * Replaces the old free-floating node "whiteboard" with a structured command
 * center for the shared blackboard and the debate on it:
 *
 *   • Stats header — phase, round, live entry counts + type distribution.
 *   • Toolbar — view switch (Timeline / Threads / Graph), group-by, type &
 *     author filters, status toggle, and search.
 *   • Timeline — entries grouped by round / type / author, newest debate
 *     activity surfaced as scannable cards (how the board evolves over turns).
 *   • Threads — ref-linked debate clusters (proposal → critique → rebuttal →
 *     resolution).
 *   • Graph — the relationship map (retained for spatial reasoning).
 *   • Detail drawer — full entry body, salience/confidence, in/out references.
 *
 * Data is sourced through useBoardEntries, which merges the live SSE stream
 * with the durable Redis snapshot so content never disappears.
 */

import React, { useMemo, useState } from "react";
import {
  LayoutList,
  Search,
  Filter,
  Inbox,
} from "lucide-react";
import { authorColor } from "@/lib/design-tokens";
import type { BoardEntry, ConsensusState, TurnRecord } from "@/hooks/useTaskStream";
import { MultiSelectDropdown } from "@/components/ui/MultiSelectDropdown";
import {
  useBoardEntries,
  groupEntries,
  typeMeta,
  prettyAuthor,
  TYPE_ORDER,
  type GroupMode,
} from "./board/boardModel";
import { BoardEntryCard } from "./board/BoardEntryCard";
import { BoardEntryDetail } from "./board/BoardEntryDetail";


interface BlackboardBoardProps {
  taskId: string;
  liveEntries: BoardEntry[];
  removedEntryIds: string[];
  isLive: boolean;
  phase?: string | null;
  consensus?: ConsensusState | null;
  allTurns?: TurnRecord[];
}

export function BlackboardBoard({
  taskId,
  liveEntries,
  removedEntryIds,
  isLive,
  phase,
  consensus,
  allTurns = [],
}: BlackboardBoardProps) {
  const { entries, synced } = useBoardEntries(taskId, liveEntries, removedEntryIds, isLive);

  const [groupMode, setGroupMode] = useState<GroupMode>("round");
  const [typeFilter, setTypeFilter] = useState<Set<string>>(new Set());
  const [modelFilter, setModelFilter] = useState<Set<string>>(new Set());
  const [authorFilter, setAuthorFilter] = useState<Set<string>>(new Set());
  const [showRemoved, setShowRemoved] = useState(false);
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // ── Derived stats (over the full board) ─────────────────────────────
  const stats = useMemo(() => {
    const typeCounts = new Map<string, number>();
    const modelCounts = new Map<string, number>();
    const entryMeta = new Map<string, { model: string }>();
    const authors = new Map<string, number>();
    let open = 0;

    for (const e of entries) {
      const turn = allTurns.find((t) => t.actor === e.author && (t.round_no === e.round || e.round === 0));
      const entryModel = e.type === "objective" ? "Input" : (turn?.model || "Unknown");
      entryMeta.set(e.id, { model: entryModel });

      typeCounts.set(e.type, (typeCounts.get(e.type) ?? 0) + 1);
      modelCounts.set(entryModel, (modelCounts.get(entryModel) ?? 0) + 1);
      authors.set(e.author, (authors.get(e.author) ?? 0) + 1);

      if (e.status === "open") open += 1;
    }

    // Ensure all models from allTurns are at least present in modelCounts with 0 entries
    // so they appear in the filter dropdown.
    for (const turn of allTurns) {
      if (turn.model && !modelCounts.has(turn.model)) {
        modelCounts.set(turn.model, 0);
      }
    }
    if (!modelCounts.has("Input")) {
      modelCounts.set("Input", 0);
    }

    const maxRound = entries.reduce((m, e) => Math.max(m, e.round), 0);
    return { typeCounts, modelCounts, entryMeta, authors, open, total: entries.length, maxRound };
  }, [entries, allTurns]);

  // ── Filter pipeline ──────────────────────────────────────────────────
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return entries.filter((e) => {
      if (!showRemoved && e.status === "removed") return false;
      if (typeFilter.size && !typeFilter.has(e.type)) return false;
      if (authorFilter.size && !authorFilter.has(e.author)) return false;
      
      const meta = stats.entryMeta.get(e.id);
      if (modelFilter.size && meta && !modelFilter.has(meta.model)) return false;

      if (q && !(`${e.title} ${e.body}`.toLowerCase().includes(q))) return false;
      return true;
    });
  }, [entries, showRemoved, typeFilter, modelFilter, authorFilter, search, stats.entryMeta]);

  const groups = useMemo(
    () => groupEntries(filtered, groupMode),
    [filtered, groupMode],
  );

  const selected = useMemo(
    () => entries.find((e) => e.id === selectedId) ?? null,
    [entries, selectedId],
  );

  const toggle = (set: Set<string>, key: string): Set<string> => {
    const next = new Set(set);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    return next;
  };

  const presentTypes = TYPE_ORDER.filter((t) => stats.typeCounts.has(t)).concat(
    [...stats.typeCounts.keys()].filter((t) => !TYPE_ORDER.includes(t)),
  );

  const empty = entries.length === 0;

  return (
    <div className="bb-board" style={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0 }}>
      {/* ── Stats header ─────────────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-3)",
          padding: "var(--space-2) var(--space-1)",
          flexWrap: "wrap",
          flexShrink: 0,
        }}
      >
        <Stat label="Phase" value={phase || "—"} accent />
        <Stat label="Round" value={stats.maxRound === 0 ? "Genesis" : `R${stats.maxRound}`} />
        <Stat label="Entries" value={`${stats.total}`} />
        <Stat label="Open" value={`${stats.open}`} />
        {consensus && (
          <Stat
            label="Consensus"
            value={`${Math.round((consensus.signal ?? 0) * 100)}%`}
          />
        )}

        {/* type distribution bar */}
        {!empty && (
          <div style={{ display: "flex", alignItems: "center", gap: 6, flex: 1, minWidth: 120, justifyContent: "flex-end" }}>
            <div style={{ display: "flex", height: 8, borderRadius: "var(--radius-full)", overflow: "hidden", width: "min(280px, 100%)", background: "var(--surface-active)" }}>
              {presentTypes.map((t) => {
                const count = stats.typeCounts.get(t) ?? 0;
                const pct = (count / stats.total) * 100;
                return (
                  <div
                    key={t}
                    title={`${typeMeta(t).label}: ${count}`}
                    style={{ width: `${pct}%`, background: typeMeta(t).color }}
                  />
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* ── Toolbar ──────────────────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-2)",
          padding: "var(--space-2) 0 var(--space-3)",
          borderBottom: "1px solid var(--border-default)",
          flexShrink: 0,
        }}
      >
        {/* row 1: group mode + search */}
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap" }}>
          <LayoutList size={14} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
          <Segmented
            options={[
              { key: "round", label: "Round" },
              { key: "type", label: "Type" },
              { key: "author", label: "Author" },
            ]}
            value={groupMode}
            onChange={(v) => setGroupMode(v as GroupMode)}
            subtle
          />


          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "4px 10px",
              borderRadius: "var(--radius-md)",
              background: "var(--surface-overlay)",
              border: "1px solid var(--border-subtle)",
              flex: 2,
              minWidth: "250px",
            }}
          >
            <Search size={13} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
            <input
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search board…"
              style={{
                background: "transparent",
                border: "none",
                outline: "none",
                color: "var(--text-primary)",
                fontSize: "var(--text-xs)",
                width: "100%",
              }}
            />
          </div>
        </div>

        {/* row 2: dropdown filters + status toggle */}
        <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", flexWrap: "wrap", zIndex: 10 }}>
          <Filter size={12} style={{ color: "var(--text-tertiary)", flexShrink: 0 }} />
          
          <MultiSelectDropdown
            label="Phase"
            options={presentTypes.map((t) => {
              const m = typeMeta(t);
              const Icon = m.icon;
              return {
                value: t,
                label: (
                  <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                    <Icon size={12} style={{ color: m.color }} />
                    {m.label}
                  </span>
                ),
                count: stats.typeCounts.get(t),
              };
            })}
            selected={typeFilter}
            onChange={setTypeFilter}
            color="hsl(217, 91%, 62%)"
          />

          <MultiSelectDropdown
            label="Agent"
            options={[...stats.authors.entries()].sort((a, b) => a[0].localeCompare(b[0])).map(([a, count]) => ({
              value: a,
              label: (
                <span style={{ display: "flex", alignItems: "center", gap: 6 }}>
                  <span style={{ width: 7, height: 7, borderRadius: "var(--radius-full)", background: authorColor(a) }} />
                  {prettyAuthor(a)}
                </span>
              ),
              count,
            }))}
            selected={authorFilter}
            onChange={setAuthorFilter}
            color="hsl(265, 60%, 66%)"
          />

          <MultiSelectDropdown
            label="Model"
            options={[...stats.modelCounts.entries()].map(([val, count]) => ({
              value: val,
              label: val === "Input" ? "Input" : val.replace(/-/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()),
              count,
            }))}
            selected={modelFilter}
            onChange={setModelFilter}
            color="hsl(32, 88%, 58%)"
          />

          <span style={{ flex: 1 }} />

          <button
            type="button"
            onClick={() => setShowRemoved((v) => !v)}
            style={{
              padding: "3px 9px",
              borderRadius: "var(--radius-full)",
              border: `1px solid ${showRemoved ? "var(--status-error)" : "var(--border-subtle)"}`,
              background: "transparent",
              color: showRemoved ? "var(--status-error)" : "var(--text-tertiary)",
              cursor: "pointer",
              fontSize: "var(--text-xs)",
              flexShrink: 0,
            }}
          >
            {showRemoved ? "Hide removed" : "Show removed"}
          </button>
        </div>
      </div>

      {/* ── Body ─────────────────────────────────────────────────────── */}
      <div style={{ flex: 1, minHeight: 0, position: "relative", overflow: "hidden" }}>
        {empty ? (
          <EmptyBoard synced={synced} isLive={isLive} />
        ) : (
          <div style={{ position: "absolute", inset: 0, overflowY: "auto", padding: "var(--space-3) var(--space-1)" }}>
            <TimelineView groups={groups} selectedId={selectedId} onSelect={setSelectedId} groupMode={groupMode} entryMeta={stats.entryMeta} />
            {filtered.length === 0 && (
              <div style={{ textAlign: "center", color: "var(--text-tertiary)", padding: "var(--space-8)", fontSize: "var(--text-sm)" }}>
                No entries match the current filters.
              </div>
            )}
          </div>
        )}

        {/* Detail drawer */}
        {selected && (
          <BoardEntryDetail
            entry={selected}
            allEntries={entries}
            onClose={() => setSelectedId(null)}
            onSelect={(id) => setSelectedId(id)}
          />
        )}
      </div>
    </div>
  );
}

// ── Timeline view ───────────────────────────────────────────────────────

function TimelineView({
  groups,
  selectedId,
  onSelect,
  groupMode,
  entryMeta,
}: {
  groups: ReturnType<typeof groupEntries>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  groupMode: GroupMode;
  entryMeta: Map<string, { model: string }>;
}) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-4)" }}>
      {groups.map((g) => (
        <section key={g.key} style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
          <GroupHeader label={g.label} sublabel={g.sublabel} mode={groupMode} groupKey={g.key} />
          <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-2)" }}>
            {g.entries.map((e) => (
              <BoardEntryCard key={e.id} entry={e} selected={selectedId === e.id} onSelect={onSelect} model={entryMeta.get(e.id)?.model} />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function GroupHeader({
  label,
  sublabel,
  mode,
  groupKey,
}: {
  label: string;
  sublabel?: string;
  mode: GroupMode;
  groupKey: string;
}) {
  const typeColor = mode === "type" ? typeMeta(groupKey).color : "var(--text-tertiary)";
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "var(--space-2)", position: "sticky", top: "calc(-1 * var(--space-3))", background: "var(--surface-base)", padding: "var(--space-1) 0", zIndex: 1 }}>
      <span
        style={{
          fontSize: "var(--text-xs)",
          fontWeight: "var(--weight-semibold)",
          color: mode === "type" ? typeColor : "var(--text-secondary)",
          textTransform: "uppercase",
          letterSpacing: "0.05em",
        }}
      >
        {label}
      </span>
      {sublabel && (
        <span
          style={{
            fontSize: "10px",
            fontFamily: "var(--font-mono)",
            color: "var(--text-tertiary)",
            background: "var(--surface-overlay)",
            padding: "1px 6px",
            borderRadius: "var(--radius-full)",
          }}
        >
          {sublabel}
        </span>
      )}
      <div style={{ flex: 1, height: 1, background: "var(--border-subtle)" }} />
    </div>
  );
}

// ── Small UI atoms ───────────────────────────────────────────────────────

function Stat({ label, value, accent }: { label: string; value: string; accent?: boolean }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
      <span style={{ fontSize: "10px", textTransform: "uppercase", letterSpacing: "0.06em", color: "var(--text-tertiary)" }}>
        {label}
      </span>
      <span
        style={{
          fontSize: "var(--text-sm)",
          fontWeight: "var(--weight-semibold)",
          color: accent ? "var(--accent-primary)" : "var(--text-primary)",
          fontFamily: "var(--font-mono)",
          textTransform: "capitalize",
        }}
      >
        {value}
      </span>
    </div>
  );
}

interface SegOption {
  key: string;
  label: string;
  icon?: React.ComponentType<{ size?: number; style?: React.CSSProperties }>;
}

function Segmented({
  options,
  value,
  onChange,
  subtle,
}: {
  options: SegOption[];
  value: string;
  onChange: (v: string) => void;
  subtle?: boolean;
}) {
  return (
    <div
      style={{
        display: "inline-flex",
        padding: 2,
        borderRadius: "var(--radius-md)",
        background: "var(--surface-overlay)",
        border: "1px solid var(--border-subtle)",
        gap: 2,
      }}
    >
      {options.map((o) => {
        const active = value === o.key;
        const Icon = o.icon;
        return (
          <button
            key={o.key}
            type="button"
            onClick={() => onChange(o.key)}
            style={{
              display: "inline-flex",
              alignItems: "center",
              gap: 5,
              padding: subtle ? "3px 8px" : "4px 10px",
              borderRadius: "var(--radius-sm)",
              border: "none",
              background: active ? "var(--surface-active)" : "transparent",
              color: active ? "var(--text-primary)" : "var(--text-tertiary)",
              cursor: "pointer",
              fontSize: "var(--text-xs)",
              fontWeight: active ? "var(--weight-semibold)" : "var(--weight-regular)",
            }}
          >
            {Icon && <Icon size={12} />}
            {o.label}
          </button>
        );
      })}
    </div>
  );
}

function EmptyBoard({ synced, isLive }: { synced: boolean; isLive: boolean }) {
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: "var(--space-3)",
        color: "var(--text-tertiary)",
      }}
    >
      <Inbox size={32} />
      <span style={{ fontSize: "var(--text-sm)" }}>
        {!synced ? "Loading board…" : isLive ? "Waiting for the swarm to post entries…" : "No board entries were recorded for this task."}
      </span>
    </div>
  );
}

export default BlackboardBoard;
