"use client";

/**
 * ConsensusMeter — convergence progress bar (doc 08 §6).
 *
 * Slim progress bar bound to consensus events from the stream.
 * Shows the decider's convergence signal and state label.
 */

import React from "react";
import type { ConsensusState } from "@/hooks/useTaskStream";

interface ConsensusMeterProps {
  consensus: ConsensusState | null;
  isLive: boolean;
}

export function ConsensusMeter({ consensus, isLive }: ConsensusMeterProps) {
  if (!consensus && !isLive) return null;

  const signal = consensus?.signal ?? 0;
  const state = consensus?.decider_state ?? "waiting";
  const pct = Math.min(Math.max(signal * 100, 0), 100);
  const isSolved = state === "accepted" || state === "solved";

  return (
    <div
      className="consensus-meter"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-2)",
        padding: "0 var(--space-1)",
      }}
    >
      {/* Progress bar */}
      <div
        style={{
          flex: 1,
          height: 4,
          borderRadius: "var(--radius-full)",
          background: "var(--surface-hover)",
          overflow: "hidden",
          minWidth: 60,
        }}
      >
        <div
          className={isSolved ? "success-bloom" : ""}
          style={{
            height: "100%",
            width: `${pct}%`,
            borderRadius: "var(--radius-full)",
            background: isSolved ? "var(--status-success)" : "var(--accent-primary)",
            transition: "width 500ms ease-out, background 300ms ease",
          }}
        />
      </div>

      {/* State label */}
      <span
        style={{
          fontSize: "var(--text-xs)",
          fontFamily: "var(--font-mono)",
          fontVariantNumeric: "tabular-nums",
          color: isSolved ? "var(--status-success)" : "var(--text-tertiary)",
          whiteSpace: "nowrap",
          flexShrink: 0,
        }}
      >
        {pct.toFixed(0)}% · {state}
      </span>
    </div>
  );
}

export default ConsensusMeter;
