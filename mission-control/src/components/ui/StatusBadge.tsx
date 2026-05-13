"use client";

import React from "react";
import type { StatusType } from "@/lib/design-tokens";

export interface StatusBadgeProps {
  status: StatusType;
  label?: string;
}

const STATUS_LABEL_MAP: Record<StatusType, string> = {
  pending: "Pending",
  running: "Running",
  success: "Success",
  error: "Error",
  paused: "Paused",
};

const STATUS_CSS_MAP: Record<StatusType, string> = {
  pending: "var(--status-pending)",
  running: "var(--status-running)",
  success: "var(--status-success)",
  error: "var(--status-error)",
  paused: "var(--status-paused)",
};

export function StatusBadge({ status, label }: StatusBadgeProps) {
  const color = STATUS_CSS_MAP[status];
  const displayLabel = label ?? STATUS_LABEL_MAP[status];

  return (
    <span
      role="status"
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "var(--space-1)",
        padding: "2px 8px",
        borderRadius: "var(--radius-full)",
        fontSize: "var(--text-xs)",
        fontWeight: "var(--weight-medium)",
        lineHeight: "var(--leading-xs)",
        color: color,
        background: `color-mix(in srgb, ${color} 15%, transparent)`,
        whiteSpace: "nowrap",
      }}
    >
      {status === "running" && (
        <span
          className="pulse-dot"
          style={{
            width: 6,
            height: 6,
            borderRadius: "var(--radius-full)",
            background: color,
            flexShrink: 0,
          }}
        />
      )}
      {displayLabel}
    </span>
  );
}

export default StatusBadge;
