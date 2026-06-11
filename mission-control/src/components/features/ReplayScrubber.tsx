"use client";

/**
 * ReplayScrubber — timeline scrubber for the BlackboardGraph (doc 08 §7).
 *
 * For completed tasks: drag to fold board_events up to seq N.
 * For running tasks: sits at "live"; dragging back pauses live-follow.
 */

import React, { useState, useCallback } from "react";
import type { BoardEntry } from "@/hooks/useTaskStream";

interface ReplayScrubberProps {
  entries: BoardEntry[];
  isLive: boolean;
  onSeqChange: (maxSeq: number) => void;
}

export function ReplayScrubber({
  entries,
  isLive,
  onSeqChange,
}: ReplayScrubberProps) {
  const maxSeq = entries.length > 0 ? Math.max(...entries.map((e) => e.seq)) : 0;
  const [value, setValue] = useState(maxSeq);
  const [isPaused, setIsPaused] = useState(false);

  // Update live position when new entries arrive
  React.useEffect(() => {
    if (!isPaused) setValue(maxSeq);
  }, [maxSeq, isPaused]);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const seq = parseInt(e.target.value, 10);
      setValue(seq);
      onSeqChange(seq);
      if (seq < maxSeq) {
        setIsPaused(true);
      } else {
        setIsPaused(false);
      }
    },
    [maxSeq, onSeqChange],
  );

  const handleResumeLive = useCallback(() => {
    setIsPaused(false);
    setValue(maxSeq);
    onSeqChange(maxSeq);
  }, [maxSeq, onSeqChange]);

  if (entries.length <= 1) return null;

  return (
    <div
      className="replay-scrubber"
      style={{
        display: "flex",
        alignItems: "center",
        gap: "var(--space-2)",
        padding: "var(--space-2) var(--space-3)",
        background: "var(--surface-hover)",
        borderRadius: "var(--radius-sm)",
        flexShrink: 0,
      }}
    >
      {/* Position label */}
      <span
        style={{
          fontSize: "var(--text-xs)",
          fontFamily: "var(--font-mono)",
          fontVariantNumeric: "tabular-nums",
          color: "var(--text-tertiary)",
          minWidth: 40,
        }}
      >
        {value}/{maxSeq}
      </span>

      {/* Range slider */}
      <input
        type="range"
        min={0}
        max={maxSeq}
        value={value}
        onChange={handleChange}
        style={{
          flex: 1,
          accentColor: "var(--accent-primary)",
          cursor: "pointer",
          height: 4,
        }}
      />

      {/* Live/Paused indicator */}
      {isLive && (
        <button
          onClick={isPaused ? handleResumeLive : undefined}
          style={{
            fontSize: "var(--text-xs)",
            fontWeight: "var(--weight-medium)",
            padding: "1px 6px",
            borderRadius: "var(--radius-sm)",
            border: "none",
            cursor: isPaused ? "pointer" : "default",
            fontFamily: "var(--font-sans)",
            background: isPaused ? "var(--status-paused)" : "var(--status-success)",
            color: "var(--text-inverse)",
            transition: "background 200ms ease",
          }}
        >
          {isPaused ? "Paused" : "Live"}
        </button>
      )}
    </div>
  );
}

export default ReplayScrubber;
