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
  systemStateStale?: boolean;
  failedDependencies?: SystemDependencyIssue[];
  affectedFeatures?: string[];
  onSystemRetry?: () => void;
  currentView?: string;
  onMenuToggle?: () => void;
  menuOpen?: boolean;
  menuButtonRef?: Ref<HTMLButtonElement>;
  inert?: boolean;
}

export function TopBar({
  systemState = "connecting",
  systemStateStale = false,
  failedDependencies = [],
  affectedFeatures = [],
  onSystemRetry = () => {},
  currentView = "Overview",
  onMenuToggle,
  menuOpen = false,
  menuButtonRef,
  inert = false,
}: TopBarProps) {
  return (
    <header className="topbar" inert={inert}>
      {/* ── Left: Hamburger + Title + Breadcrumb ──────────────────── */}
      <div className="topbar__left">
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
          <h1 className="topbar__title">Stigmergic</h1>
        </Link>

        <span className="topbar__breadcrumb-sep" aria-hidden="true">/</span>
        <span className="topbar__breadcrumb">{currentView}</span>
      </div>

      {/* ── Right: System status ───────────────────────────────────── */}
      <div className="topbar__right">
        <SystemStatusPanel
          state={systemState}
          isStale={systemStateStale}
          failedDependencies={failedDependencies}
          affectedFeatures={affectedFeatures}
          onRetry={onSystemRetry}
        />
      </div>
    </header>
  );
}

export default TopBar;
