"use client";

/**
 * AgentTrace — structured trace timeline (doc 09 §3).
 *
 * Grouped by turn (newest pinned, scrollable). Smart auto-scroll
 * with "↓ N new" pill. Virtualized for performance.
 */

import React, { useRef, useState, useEffect, useCallback, useMemo } from "react";
import { useVirtualizer } from "@tanstack/react-virtual";
import { authorColor } from "@/lib/design-tokens";
import type { TraceEvent, TurnRecord } from "@/hooks/useTaskStream";
import { ToolCallCard } from "./ToolCallCard";
import { Activity } from "lucide-react";

// ── Trace line glyph map (doc 09 §3) ─────────────────────────────────

const TYPE_GLYPHS: Record<string, { glyph: string; color: string }> = {
  reasoning:      { glyph: "●", color: "var(--accent-primary)" },
  tool_call:      { glyph: "▸", color: "var(--status-paused)" },
  entries_posted: { glyph: "◆", color: "var(--status-success)" },
  final:          { glyph: "✓", color: "var(--status-success)" },
  error:          { glyph: "✕", color: "var(--status-error)" },
};

function getGlyph(type: string) {
  return TYPE_GLYPHS[type] ?? { glyph: "·", color: "var(--text-tertiary)" };
}

// ── Group traces by turn ──────────────────────────────────────────────

interface TurnGroup {
  turnId: string;
  actor: string;
  turn?: TurnRecord;
  traces: TraceEvent[];
}

function groupByTurn(
  traces: TraceEvent[],
  activeTurns: TurnRecord[],
  completedTurns: TurnRecord[],
): TurnGroup[] {
  const turnMap = new Map<string, TurnRecord>();
  for (const t of [...activeTurns, ...completedTurns]) {
    turnMap.set(t.turn_id, t);
  }

  const groups = new Map<string, TurnGroup>();
  for (const trace of traces) {
    const key = trace.turn_id || `orphan-${trace.actor}`;
    if (!groups.has(key)) {
      groups.set(key, {
        turnId: key,
        actor: trace.actor,
        turn: turnMap.get(trace.turn_id),
        traces: [],
      });
    }
    groups.get(key)!.traces.push(trace);
  }

  // Newest first
  return Array.from(groups.values()).reverse();
}

// ── Props ─────────────────────────────────────────────────────────────

interface AgentTraceProps {
  traceEvents: TraceEvent[];
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  onTurnClick?: (turnId: string) => void;
}

// ── Component ─────────────────────────────────────────────────────────

export function AgentTrace({
  traceEvents,
  activeTurns,
  completedTurns,
  onTurnClick,
}: AgentTraceProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const [isNearBottom, setIsNearBottom] = useState(true);
  const [newCount, setNewCount] = useState(0);
  const prevLen = useRef(traceEvents.length);

  const groups = useMemo(
    () => groupByTurn(traceEvents, activeTurns, completedTurns),
    [traceEvents, activeTurns, completedTurns],
  );

  // Flatten for virtualizer
  const flatItems = useMemo(() => {
    const items: ({ kind: "header"; group: TurnGroup } | { kind: "trace"; trace: TraceEvent; group: TurnGroup })[] = [];
    for (const g of groups) {
      items.push({ kind: "header", group: g });
      for (const t of g.traces) {
        items.push({ kind: "trace", trace: t, group: g });
      }
    }
    return items;
  }, [groups]);

  const virtualizer = useVirtualizer({
    count: flatItems.length,
    getScrollElement: () => scrollRef.current,
    estimateSize: (i) => (flatItems[i].kind === "header" ? 36 : 28),
    overscan: 30,
  });

  // Smart auto-scroll
  useEffect(() => {
    const added = traceEvents.length - prevLen.current;
    prevLen.current = traceEvents.length;
    if (added > 0) {
      if (isNearBottom && scrollRef.current) {
        scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
      } else {
        setNewCount((p) => p + added);
      }
    }
  }, [traceEvents.length, isNearBottom]);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
    setIsNearBottom(near);
    if (near) setNewCount(0);
  }, []);

  const snapToBottom = useCallback(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    setNewCount(0);
  }, []);

  if (traceEvents.length === 0) {
    return (
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          height: "100%",
          gap: "var(--space-3)",
          color: "var(--text-tertiary)",
          padding: "var(--space-8)",
        }}
      >
        <Activity size={28} />
        <span style={{ fontSize: "var(--text-sm)" }}>
          Waiting for trace events…
        </span>
      </div>
    );
  }

  return (
    <div style={{ position: "relative", height: "100%", display: "flex", flexDirection: "column" }}>
      <div
        ref={scrollRef}
        onScroll={handleScroll}
        style={{
          flex: 1,
          overflow: "auto",
          padding: "var(--space-2)",
        }}
      >
        <div
          style={{
            height: virtualizer.getTotalSize(),
            width: "100%",
            position: "relative",
          }}
        >
          {virtualizer.getVirtualItems().map((vItem) => {
            const item = flatItems[vItem.index];
            if (item.kind === "header") {
              const g = item.group;
              const color = authorColor(g.actor);
              return (
                <div
                  key={vItem.key}
                  ref={virtualizer.measureElement}
                  data-index={vItem.index}
                  style={{
                    position: "absolute",
                    top: 0,
                    left: 0,
                    width: "100%",
                    transform: `translateY(${vItem.start}px)`,
                  }}
                >
                  <button
                    onClick={() => onTurnClick?.(g.turnId)}
                    className="trace-turn-header"
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "var(--space-2)",
                      padding: "var(--space-1) var(--space-2)",
                      background: "var(--surface-hover)",
                      borderRadius: "var(--radius-sm)",
                      border: "none",
                      cursor: "pointer",
                      width: "100%",
                      fontFamily: "var(--font-sans)",
                      color: "inherit",
                      textAlign: "left",
                      marginBottom: "var(--space-1)",
                    }}
                  >
                    <span style={{ width: 8, height: 8, borderRadius: "var(--radius-full)", background: color, flexShrink: 0 }} />
                    <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-semibold)", color: "var(--text-primary)", textTransform: "capitalize" }}>
                      {g.actor.replace(/_/g, " ")}
                    </span>
                    {g.turn && (
                      <>
                        <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums" }}>
                          R{g.turn.round_no}
                        </span>
                        <span
                          style={{
                            fontSize: "var(--text-xs)",
                            padding: "0 4px",
                            borderRadius: "var(--radius-sm)",
                            background: g.turn.status === "active" ? "var(--accent-subtle)" : "var(--surface-active)",
                            color: g.turn.status === "active" ? "var(--accent-primary)" : "var(--text-secondary)",
                          }}
                        >
                          {g.turn.phase}
                        </span>
                        {g.turn.tokens_in != null && (
                          <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", marginLeft: "auto" }}>
                            {((g.turn.tokens_in ?? 0) + (g.turn.tokens_out ?? 0)).toLocaleString()} tok
                          </span>
                        )}
                      </>
                    )}
                  </button>
                </div>
              );
            }

            // Trace line
            const trace = item.trace;
            const { glyph, color } = getGlyph(trace.type);
            const isToolCall = trace.type === "tool_call";

            return (
              <div
                key={vItem.key}
                ref={virtualizer.measureElement}
                data-index={vItem.index}
                style={{
                  position: "absolute",
                  top: 0,
                  left: 0,
                  width: "100%",
                  transform: `translateY(${vItem.start}px)`,
                }}
              >
                <div
                  className="trace-line"
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: "var(--space-2)",
                    padding: "1px var(--space-2) 1px var(--space-4)",
                    fontSize: "var(--text-sm)",
                    lineHeight: "var(--leading-sm)",
                  }}
                >
                  {/* Gutter glyph */}
                  <span
                    style={{
                      flexShrink: 0,
                      width: 14,
                      textAlign: "center",
                      color,
                      fontFamily: "var(--font-mono)",
                      fontSize: "var(--text-xs)",
                      lineHeight: "var(--leading-sm)",
                    }}
                  >
                    {glyph}
                  </span>

                  {/* Timestamp */}
                  <span
                    style={{
                      flexShrink: 0,
                      fontSize: "var(--text-xs)",
                      color: "var(--text-tertiary)",
                      fontFamily: "var(--font-mono)",
                      fontVariantNumeric: "tabular-nums",
                      minWidth: 60,
                    }}
                  >
                    {new Date(trace.timestamp).toLocaleTimeString()}
                  </span>

                  {/* Body */}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {isToolCall ? (
                      <ToolCallCard content={trace.content} />
                    ) : (
                      <span
                        style={{
                          color: "var(--text-secondary)",
                          wordBreak: "break-word",
                        }}
                      >
                        {trace.content.length > 300
                          ? trace.content.slice(0, 300) + "…"
                          : trace.content}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* "N new" pill */}
      {newCount > 0 && (
        <button
          className="new-output-pill"
          onClick={snapToBottom}
          style={{
            position: "absolute",
            bottom: "var(--space-3)",
            left: "50%",
            transform: "translateX(-50%)",
          }}
        >
          ↓ {newCount} new event{newCount !== 1 ? "s" : ""}
        </button>
      )}
    </div>
  );
}

export default AgentTrace;
