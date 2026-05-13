"use client";

import React from "react";
import { StatusBadge } from "./ui/StatusBadge";
import type { StatusType } from "@/lib/design-tokens";

export interface TopBarProps {
  daemonStatus?: StatusType;
  swarmPhase?: string;
  totalCost?: number;
}

export function TopBar({
  daemonStatus = "pending",
  swarmPhase,
  totalCost = 0,
}: TopBarProps) {
  const costFormatted = totalCost.toFixed(4);

  return (
    <header
      style={{
        height: "var(--topbar-height)",
        minHeight: "var(--topbar-height)",
        background: "var(--surface-raised)",
        borderBottom: "1px solid var(--border-default)",
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "0 var(--space-5)",
        flexShrink: 0,
        zIndex: 100,
      }}
    >
      {/* ── Left: Title + Daemon Status ────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-3)",
        }}
      >
        <h1
          style={{
            fontSize: "var(--text-lg)",
            fontWeight: "var(--weight-semibold)",
            lineHeight: "var(--leading-lg)",
            color: "var(--text-primary)",
            margin: 0,
            whiteSpace: "nowrap",
          }}
        >
          Mission Control
        </h1>
        <StatusBadge
          status={daemonStatus}
          label={
            daemonStatus === "running"
              ? "Connected"
              : daemonStatus === "error"
                ? "Disconnected"
                : daemonStatus === "paused"
                  ? "Paused"
                  : "Connecting…"
          }
        />
      </div>

      {/* ── Center: Swarm Phase ────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-2)",
          position: "absolute",
          left: "50%",
          transform: "translateX(-50%)",
        }}
      >
        {swarmPhase ? (
          <span
            style={{
              fontSize: "var(--text-sm)",
              color: "var(--text-secondary)",
              fontWeight: "var(--weight-medium)",
              lineHeight: "var(--leading-sm)",
            }}
          >
            {swarmPhase}
          </span>
        ) : (
          <span
            style={{
              fontSize: "var(--text-sm)",
              color: "var(--text-tertiary)",
              fontStyle: "italic",
            }}
          >
            No active session
          </span>
        )}
      </div>

      {/* ── Right: Cost Ticker ─────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: "var(--space-1)",
        }}
      >
        <span
          style={{
            fontSize: "var(--text-mono)",
            fontFamily: "var(--font-mono)",
            color: "var(--text-tertiary)",
          }}
        >
          $
        </span>
        <span
          style={{
            fontSize: "var(--text-mono)",
            fontFamily: "var(--font-mono)",
            fontWeight: "var(--weight-regular)",
            color: "var(--text-primary)",
            lineHeight: "var(--leading-mono)",
          }}
        >
          {costFormatted}
        </span>
      </div>
    </header>
  );
}

export default TopBar;
