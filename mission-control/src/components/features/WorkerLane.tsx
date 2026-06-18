"use client";

/**
 * WorkerLane — horizontal agent activity strip (doc 08 §4).
 *
 * Shows one card per agent role with live activity indicators.
 * Active turns show role + activity verb + token counter + cost.
 * Idle agents render muted.
 */

import React, { useMemo } from "react";
import { authorColor } from "@/lib/design-tokens";
import type { TurnRecord, TraceEvent, BoardEntry } from "@/hooks/useTaskStream";

interface WorkerLaneProps {
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  traceEvents: TraceEvent[];
  boardEntries: BoardEntry[];
  onTurnClick?: (turnId: string) => void;
}

export function WorkerLane({
  activeTurns,
  completedTurns,
  traceEvents,
  boardEntries,
  onTurnClick,
}: WorkerLaneProps) {
  // Build roster from all known actors
  const roster = useMemo(() => {
    const actors = new Set<string>();
    for (const t of activeTurns) actors.add(t.actor);
    for (const t of completedTurns) actors.add(t.actor);
    for (const e of boardEntries) actors.add(e.author);
    return Array.from(actors).sort();
  }, [activeTurns, completedTurns, boardEntries]);

  // Latest trace per actor for activity verb
  const latestTrace = useMemo(() => {
    const map = new Map<string, TraceEvent>();
    for (const t of traceEvents) {
      const existing = map.get(t.actor);
      if (!existing || t.seq > existing.seq) {
        map.set(t.actor, t);
      }
    }
    return map;
  }, [traceEvents]);

  // Active turn lookup
  const activeByActor = useMemo(() => {
    const map = new Map<string, TurnRecord>();
    for (const t of activeTurns) map.set(t.actor, t);
    return map;
  }, [activeTurns]);

  if (roster.length === 0) {
    return null; // No agents yet
  }

  return (
    <div
      className="worker-lane"
      style={{
        display: "flex",
        gap: "var(--space-2)",
        overflowX: "auto",
        padding: "var(--space-2) 0",
        flexShrink: 0,
        scrollbarWidth: "thin",
      }}
    >
      {roster.map((actor) => {
        const isActive = activeByActor.has(actor);
        const turn = activeByActor.get(actor);
        const trace = latestTrace.get(actor);
        const color = authorColor(actor);

        return (
          <button
            key={actor}
            className="worker-lane-card"
            onClick={() => turn && onTurnClick?.(turn.turn_id)}
            style={{
              display: "flex",
              alignItems: "center",
              gap: "var(--space-2)",
              padding: "var(--space-2) var(--space-3)",
              background: isActive ? "var(--surface-overlay)" : "var(--surface-hover)",
              borderRadius: "var(--radius-md)",
              border: isActive ? `1px solid ${color}` : "1px solid var(--border-subtle)",
              opacity: isActive ? 1 : 0.5,
              cursor: turn ? "pointer" : "default",
              flexShrink: 0,
              minWidth: 140,
              transition: "opacity 200ms ease, border-color 200ms ease",
              fontFamily: "var(--font-sans)",
              color: "inherit",
              textAlign: "left",
            }}
          >
            {/* Agent dot */}
            <span
              className={isActive ? "pulse-dot" : ""}
              style={{
                width: 8,
                height: 8,
                borderRadius: "var(--radius-full)",
                background: color,
                flexShrink: 0,
              }}
            />

            {/* Info */}
            <div style={{ flex: 1, minWidth: 0, display: "flex", flexDirection: "column", gap: 1 }}>
              <span
                style={{
                  fontSize: "var(--text-xs)",
                  fontWeight: "var(--weight-semibold)",
                  color: isActive ? "var(--text-primary)" : "var(--text-tertiary)",
                  textTransform: "capitalize",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  whiteSpace: "nowrap",
                }}
              >
                {/** Strip "expert." / "worker." prefix and titlecase the slug. */}
                {(actor.includes(".")
                  ? actor.split(".").slice(1).join(".")
                  : actor
                ).replace(/_/g, " ")}
              </span>
              {isActive && trace && (
                <span
                  style={{
                    fontSize: "var(--text-xs)",
                    color: "var(--text-secondary)",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                >
                  {trace.type}
                </span>
              )}
              {!isActive && (
                <span
                  style={{
                    fontSize: "var(--text-xs)",
                    color: "var(--text-tertiary)",
                    fontStyle: "italic",
                  }}
                >
                  idle
                </span>
              )}
            </div>

            {/* Token counter */}
            {isActive && turn && (
              <span
                style={{
                  fontSize: "var(--text-xs)",
                  fontFamily: "var(--font-mono)",
                  fontVariantNumeric: "tabular-nums",
                  color: "var(--text-tertiary)",
                  flexShrink: 0,
                }}
              >
                R{turn.round_no}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

export default WorkerLane;
