"use client";



import { Menu } from "lucide-react";
import Link from "next/link";
import type { Ref } from "react";
import { SystemStatusPanel } from "./SystemStatusPanel";
import type {
  SystemConnectionState,
  SystemDependencyIssue,
} from "@/hooks/useSystemStream";

export interface TopBarProps {
  systemState?: SystemConnectionState;
  lastSuccessfulEventAt?: string | null;
  systemStateStale?: boolean;
  failedDependencies?: SystemDependencyIssue[];
  affectedFeatures?: string[];
  onSystemRetry?: () => void;
  swarmPhase?: string;
  totalCost?: number;
  currentView?: string;
  onMenuToggle?: () => void;
  menuOpen?: boolean;
  menuButtonRef?: Ref<HTMLButtonElement>;
  inert?: boolean;
}

export function TopBar({
  systemState = "connecting",
  lastSuccessfulEventAt = null,
  systemStateStale = false,
  failedDependencies = [],
  affectedFeatures = [],
  onSystemRetry = () => {},
  swarmPhase,
  totalCost = 0,
  currentView = "Overview",
  onMenuToggle,
  menuOpen = false,
  menuButtonRef,
  inert = false,
}: TopBarProps) {
  const costFormatted = totalCost.toFixed(4);

  return (
    <header className="topbar" inert={inert}>
      {/* ── Left: Hamburger + Title + Status ──────────────────────── */}
      <div className="topbar__left">
        {/* Mobile hamburger */}
        <button
          ref={menuButtonRef}
          type="button"
          className="topbar__menu-btn"
          onClick={onMenuToggle}
          aria-label={menuOpen ? "Close navigation menu" : "Open navigation menu"}
          aria-controls="primary-navigation"
          aria-expanded={menuOpen}
        >
          <Menu size={20} />
        </button>

        <Link href="/" className="topbar__title-link">
          <h1 className="topbar__title">
            <span className="topbar__title-full">Stigmergic</span>
            <span className="topbar__title-short">Stigmergic</span>
          </h1>
        </Link>

        <SystemStatusPanel
          state={systemState}
          lastSuccessfulEventAt={lastSuccessfulEventAt}
          isStale={systemStateStale}
          failedDependencies={failedDependencies}
          affectedFeatures={affectedFeatures}
          onRetry={onSystemRetry}
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
        <span className="topbar__cost-label">Visible task cost</span>
        <span className="topbar__cost-sign">$</span>
        <span className="topbar__cost-value">{costFormatted}</span>
      </div>
    </header>
  );
}

export default TopBar;
