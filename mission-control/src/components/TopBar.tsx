"use client";

import React from "react";
import { StatusBadge } from "./ui/StatusBadge";
import type { StatusType } from "@/lib/design-tokens";
import { Menu } from "lucide-react";

export interface TopBarProps {
  daemonStatus?: StatusType;
  swarmPhase?: string;
  totalCost?: number;
  currentView?: string;
  onMenuToggle?: () => void;
}

export function TopBar({
  daemonStatus = "pending",
  swarmPhase,
  totalCost = 0,
  currentView = "Overview",
  onMenuToggle,
}: TopBarProps) {
  const costFormatted = totalCost.toFixed(4);

  return (
    <header className="topbar">
      {/* ── Left: Hamburger + Title + Status ──────────────────────── */}
      <div className="topbar__left">
        {/* Mobile hamburger */}
        <button
          className="topbar__menu-btn"
          onClick={onMenuToggle}
          aria-label="Toggle navigation menu"
        >
          <Menu size={20} />
        </button>

        <h1 className="topbar__title">
          <span className="topbar__title-full">Mission Control</span>
          <span className="topbar__title-short">MC</span>
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

        {/* Breadcrumb separator + current view */}
        <span className="topbar__breadcrumb-sep">/</span>
        <span className="topbar__breadcrumb">{currentView}</span>
      </div>

      {/* ── Center: Swarm Phase (hidden on mobile) ─────────────── */}
      <div className="topbar__center">
        {swarmPhase ? (
          <span className="topbar__phase">{swarmPhase}</span>
        ) : (
          <span className="topbar__phase topbar__phase--idle">No active session</span>
        )}
      </div>

      {/* ── Right: Cost Ticker ─────────────────────────────────── */}
      <div className="topbar__right">
        <span className="topbar__cost-sign">$</span>
        <span className="topbar__cost-value">{costFormatted}</span>
      </div>
    </header>
  );
}

export default TopBar;
