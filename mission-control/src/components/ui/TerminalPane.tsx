"use client";

import React, { useCallback } from "react";
import type { AgentRole } from "@/lib/design-tokens";
import { AGENT_COLORS } from "@/lib/design-tokens";

// ── Types ────────────────────────────────────────────────────────────

export interface TerminalPaneProps {
  role: AgentRole;
  connected?: boolean;
  reconnecting?: boolean;
  onClear?: () => void;
  children: React.ReactNode;
}

const ROLE_LABELS: Record<AgentRole, string> = {
  planner: "Planner",
  executor: "Executor",
  auditor: "Auditor",
  critic: "Critic",
  conflict_resolver: "Conflict Resolver",
  cleaner: "Cleaner",
  decider: "Decider",
};

// ── Component ────────────────────────────────────────────────────────

export function TerminalPane({
  role,
  connected = true,
  reconnecting = false,
  onClear,
  children,
}: TerminalPaneProps) {
  const agentColor = AGENT_COLORS[role];

  const handleClear = useCallback(() => {
    onClear?.();
  }, [onClear]);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        width: "100%",
        overflow: "hidden",
        borderRadius: `0 0 var(--radius-xl) var(--radius-xl)`,
        border: "1px solid var(--border-default)",
        borderTop: `2px solid ${agentColor}`,
      }}
    >
      {/* ── Header Bar (32px) ─────────────────────────────────────── */}
      <div
        style={{
          height: 32,
          minHeight: 32,
          background: "var(--surface-raised)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "0 var(--space-3)",
        }}
      >
        {/* Left: Agent identity dot + role name */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-2)",
          }}
        >
          <span
            style={{
              width: 8,
              height: 8,
              borderRadius: "var(--radius-full)",
              background: agentColor,
              flexShrink: 0,
            }}
          />
          <span
            style={{
              fontSize: "var(--text-sm)",
              fontWeight: "var(--weight-medium)",
              color: "var(--text-primary)",
            }}
          >
            {ROLE_LABELS[role]}
          </span>
        </div>

        {/* Right: Connection status + Clear */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-3)",
          }}
        >
          {/* Connection indicator */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: "var(--space-1)",
            }}
          >
            <span
              className={reconnecting ? "pulse-dot" : undefined}
              style={{
                width: 6,
                height: 6,
                borderRadius: "var(--radius-full)",
                background: connected
                  ? "var(--status-success)"
                  : reconnecting
                    ? "var(--status-paused)"
                    : "var(--status-error)",
                flexShrink: 0,
              }}
            />
            {(!connected || reconnecting) && (
              <span
                style={{
                  fontSize: "var(--text-xs)",
                  color: reconnecting
                    ? "var(--status-paused)"
                    : "var(--status-error)",
                }}
              >
                {reconnecting ? "Reconnecting…" : "Disconnected"}
              </span>
            )}
          </div>

          {/* Clear button */}
          {onClear && (
            <button
              onClick={handleClear}
              aria-label="Clear terminal"
              style={{
                fontSize: "var(--text-xs)",
                color: "var(--text-tertiary)",
                background: "transparent",
                border: "none",
                cursor: "pointer",
                padding: "2px var(--space-2)",
                borderRadius: "var(--radius-sm)",
                transition: "color 150ms ease, background 150ms ease",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.color = "var(--text-secondary)";
                e.currentTarget.style.background = "var(--surface-hover)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.color = "var(--text-tertiary)";
                e.currentTarget.style.background = "transparent";
              }}
            >
              Clear
            </button>
          )}
        </div>
      </div>

      {/* ── Terminal Body ──────────────────────────────────────────── */}
      <div
        style={{
          flex: 1,
          minHeight: 0,
          overflow: "hidden",
          background: "var(--surface-base)",
        }}
      >
        {children}
      </div>
    </div>
  );
}

export default TerminalPane;
