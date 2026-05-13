"use client";

import React from "react";

export interface SplitViewProps {
  leftHeader: string;
  rightHeader: string;
  left: React.ReactNode;
  right: React.ReactNode;
}

export function SplitView({ leftHeader, rightHeader, left, right }: SplitViewProps) {
  return (
    <div
      style={{
        display: "flex",
        flex: 1,
        minHeight: 0,
        overflow: "hidden",
      }}
    >
      {/* ── Left Pane ──────────────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            fontSize: "var(--text-sm)",
            fontWeight: "var(--weight-medium)",
            color: "var(--text-secondary)",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
            padding: `var(--space-2) var(--space-3)`,
            flexShrink: 0,
          }}
        >
          {leftHeader}
        </div>
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflow: "auto",
            padding: `0 var(--space-3) var(--space-3)`,
          }}
        >
          {left}
        </div>
      </div>

      {/* ── Divider ───────────────────────────────────────────────── */}
      <div
        style={{
          width: 1,
          background: "var(--border-default)",
          flexShrink: 0,
        }}
      />

      {/* ── Right Pane ─────────────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          minWidth: 0,
          overflow: "hidden",
        }}
      >
        <div
          style={{
            fontSize: "var(--text-sm)",
            fontWeight: "var(--weight-medium)",
            color: "var(--text-secondary)",
            textTransform: "uppercase",
            letterSpacing: "0.05em",
            padding: `var(--space-2) var(--space-3)`,
            flexShrink: 0,
          }}
        >
          {rightHeader}
        </div>
        <div
          style={{
            flex: 1,
            minHeight: 0,
            overflow: "auto",
            padding: `0 var(--space-3) var(--space-3)`,
          }}
        >
          {right}
        </div>
      </div>
    </div>
  );
}

export default SplitView;
