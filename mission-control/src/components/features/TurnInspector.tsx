"use client";

/**
 * TurnInspector — right-side slide-over panel (doc 09 §4).
 *
 * Opens from board node click or worker card click.
 * Shows complete trace timeline for a single turn plus
 * "Resulted in" footer (board entries, rejections, artifacts).
 */

import React, { useMemo } from "react";
import { authorColor } from "@/lib/design-tokens";
import type { TurnRecord, TraceEvent, BoardEntry, RejectedEntry } from "@/hooks/useTaskStream";
import { ToolCallCard } from "./ToolCallCard";
import { X } from "lucide-react";

// ── Trace line glyph map ──────────────────────────────────────────────

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

// ── Props ─────────────────────────────────────────────────────────────

interface TurnInspectorProps {
  turnId: string | null;
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  traceEvents: TraceEvent[];
  boardEntries: BoardEntry[];
  rejectedEntries: RejectedEntry[];
  onClose: () => void;
}

// ── Component ─────────────────────────────────────────────────────────

export function TurnInspector({
  turnId,
  activeTurns,
  completedTurns,
  traceEvents,
  boardEntries,
  rejectedEntries,
  onClose,
}: TurnInspectorProps) {
  // Find turn
  const turn = useMemo(() => {
    if (!turnId) return null;
    return (
      activeTurns.find((t) => t.turn_id === turnId) ??
      completedTurns.find((t) => t.turn_id === turnId) ??
      null
    );
  }, [turnId, activeTurns, completedTurns]);

  // Filter traces for this turn
  const turnTraces = useMemo(() => {
    if (!turnId) return [];
    return traceEvents.filter((t) => t.turn_id === turnId);
  }, [turnId, traceEvents]);

  // Board entries created during this turn (matched by actor + time window)
  const createdEntries = useMemo(() => {
    if (!turn) return [];
    return boardEntries.filter(
      (e) =>
        e.author === turn.actor &&
        new Date(e.created_at).getTime() >= new Date(turn.started_at).getTime(),
    );
  }, [turn, boardEntries]);

  // Rejections during this turn
  const turnRejections = useMemo(() => {
    if (!turn) return [];
    return rejectedEntries.filter((r) => r.actor === turn.actor);
  }, [turn, rejectedEntries]);

  if (!turnId) return null;

  const actor = turn?.actor ?? turnTraces[0]?.actor ?? "unknown";
  const color = authorColor(actor);

  return (
    <>
      {/* Backdrop */}
      <div
        className="turn-inspector-backdrop"
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          background: "hsl(0 0% 0% / 0.4)",
          zIndex: 90,
          cursor: "pointer",
        }}
      />

      {/* Slide-over panel */}
      <div
        className="turn-inspector"
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: "min(460px, 90vw)",
          background: "var(--surface-raised)",
          borderLeft: "1px solid var(--border-default)",
          zIndex: 91,
          display: "flex",
          flexDirection: "column",
          animation: "toast-enter 200ms ease-out",
          overflowY: "auto",
        }}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-3)",
            padding: "var(--space-4)",
            borderBottom: "1px solid var(--border-subtle)",
            flexShrink: 0,
          }}
        >
          <span style={{ width: 10, height: 10, borderRadius: "var(--radius-full)", background: color, flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: "var(--weight-semibold)", fontSize: "var(--text-base)", textTransform: "capitalize" }}>
              {actor.replace(/_/g, " ")}
            </div>
            {turn && (
              <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
                <span>Round {turn.round_no}</span>
                <span>· {turn.phase}</span>
                <span>· {turn.status}</span>
                {turn.model && <span>· {turn.model}</span>}
              </div>
            )}
          </div>

          {/* Stats */}
          {turn && (turn.tokens_in != null || turn.cost_usd != null) && (
            <div style={{ fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", color: "var(--text-tertiary)", textAlign: "right", flexShrink: 0 }}>
              {turn.tokens_in != null && (
                <div>{((turn.tokens_in ?? 0) + (turn.tokens_out ?? 0)).toLocaleString()} tok</div>
              )}
              {turn.cost_usd != null && <div>${turn.cost_usd.toFixed(4)}</div>}
            </div>
          )}

          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              color: "var(--text-tertiary)",
              padding: "var(--space-1)",
              flexShrink: 0,
            }}
          >
            <X size={16} />
          </button>
        </div>

        {/* Trace timeline */}
        <div style={{ flex: 1, overflow: "auto", padding: "var(--space-3)" }}>
          {turnTraces.length === 0 ? (
            <div style={{ color: "var(--text-tertiary)", textAlign: "center", padding: "var(--space-6)" }}>
              No trace data for this turn.
            </div>
          ) : (
            turnTraces.map((trace) => {
              const { glyph, color: glyphColor } = getGlyph(trace.type);
              const isToolCall = trace.type === "tool_call";

              return (
                <div
                  key={trace.id}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: "var(--space-2)",
                    padding: "2px 0",
                    fontSize: "var(--text-sm)",
                  }}
                >
                  <span style={{ width: 14, textAlign: "center", color: glyphColor, fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", flexShrink: 0 }}>
                    {glyph}
                  </span>
                  <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", minWidth: 60, flexShrink: 0 }}>
                    {new Date(trace.timestamp).toLocaleTimeString()}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {isToolCall ? (
                      <ToolCallCard content={trace.content} />
                    ) : (
                      <span style={{ color: "var(--text-secondary)", wordBreak: "break-word" }}>
                        {trace.content}
                      </span>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* "Resulted in" footer */}
        {(createdEntries.length > 0 || turnRejections.length > 0) && (
          <div
            style={{
              borderTop: "1px solid var(--border-subtle)",
              padding: "var(--space-3)",
              flexShrink: 0,
            }}
          >
            <div style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-semibold)", color: "var(--text-tertiary)", marginBottom: "var(--space-2)" }}>
              Resulted in
            </div>

            {createdEntries.map((e) => (
              <div
                key={e.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "var(--space-2)",
                  padding: "2px 0",
                  fontSize: "var(--text-xs)",
                  color: "var(--text-secondary)",
                }}
              >
                <span style={{ width: 6, height: 6, borderRadius: "var(--radius-full)", background: "var(--status-success)", flexShrink: 0 }} />
                <span style={{ fontWeight: "var(--weight-medium)" }}>{e.type}</span>
                <span style={{ color: "var(--text-tertiary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {e.title || e.body.slice(0, 60)}
                </span>
              </div>
            ))}

            {turnRejections.map((r) => (
              <div
                key={r.entry_id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "var(--space-2)",
                  padding: "2px 0",
                  fontSize: "var(--text-xs)",
                  color: "var(--status-error)",
                }}
              >
                <span style={{ width: 6, height: 6, borderRadius: "var(--radius-full)", background: "var(--status-error)", flexShrink: 0 }} />
                <span>Rejected: {r.reason}</span>
              </div>
            ))}

            {/* Phase 5 placeholder */}
            {/* TODO(phase-5): Inline Approve/Deny buttons for approval_request events */}
          </div>
        )}
      </div>
    </>
  );
}

export default TurnInspector;
