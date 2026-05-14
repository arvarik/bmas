"use client";

import React, { useState, useCallback, useEffect, createContext, useContext } from "react";
import { ToastProvider } from "@/components/ui/Toast";
import { TopBar } from "@/components/TopBar";
import { Sidebar } from "@/components/ui/Sidebar";
import { useBlackboard } from "@/hooks/useBlackboard";
import type { StatusType } from "@/lib/design-tokens";

// ── Navigation Context ───────────────────────────────────────────────

interface AppShellContext {
  activeNav: string;
  setActiveNav: (id: string) => void;
}

const AppShellCtx = createContext<AppShellContext>({
  activeNav: "overview",
  setActiveNav: () => {},
});

export function useAppShell() {
  return useContext(AppShellCtx);
}

// ── Nav labels for breadcrumbs ───────────────────────────────────────

const NAV_LABELS: Record<string, string> = {
  overview: "Overview",
  dag: "Task DAG",
  logs: "Agent Logs",
  operator: "Operator",
  blackboard: "Blackboard",
  cost: "Cost & Tokens",
  infra: "Infrastructure",
  skills: "Agent Skills",
};

/**
 * AppShell — client-side layout wrapper.
 *
 * Manages sidebar collapsed state, active navigation item, TopBar
 * rendering, Toast provider, keyboard shortcuts, and responsive
 * sidebar behavior.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [activeNav, setActiveNav] = useState("overview");
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);

  // Pull live data from the blackboard store for the TopBar
  const daemonState = useBlackboard((s) => s.state);
  const daemonError = useBlackboard((s) => s.error);
  const startPolling = useBlackboard((s) => s.startPolling);

  // Start the daemon-state polling loop on mount.
  // This must live here (not in a lazily-loaded child) so the TopBar
  // gets connection status as soon as the shell renders.
  useEffect(() => {
    const cleanup = startPolling();
    return cleanup;
  }, [startPolling]);

  const daemonStatus: StatusType = daemonError
    ? "error"
    : daemonState
      ? "running"
      : "pending";

  const swarmPhase = daemonState?.phase
    ? `${daemonState.phase} — Iteration ${daemonState.iteration}`
    : undefined;

  // Compute total cost from the store (if available)
  const totalCost = 0;

  const handleToggleCollapse = useCallback(() => {
    setSidebarCollapsed((prev) => !prev);
  }, []);

  const handleNavigate = useCallback((id: string) => {
    setActiveNav(id);
    // Close mobile drawer on navigation
    setMobileDrawerOpen(false);
  }, []);

  const handleToggleMobileDrawer = useCallback(() => {
    setMobileDrawerOpen((prev) => !prev);
  }, []);

  // ── Responsive: auto-collapse sidebar at <1024px ──────────────
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 1023px)");
    const handler = (e: MediaQueryListEvent | MediaQueryList) => {
      setSidebarCollapsed(e.matches);
      if (!e.matches) setMobileDrawerOpen(false);
    };
    handler(mq);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  // ── Global keyboard shortcuts ─────────────────────────────────
  useEffect(() => {
    const NAV_KEYS: Record<string, string> = {
      "1": "overview", "2": "dag", "3": "logs", "4": "operator",
      "5": "blackboard", "6": "cost", "7": "infra", "8": "skills",
    };

    function handleKeyDown(e: KeyboardEvent) {
      // Don't hijack when user is typing in an input
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      // Esc → back to overview
      if (e.key === "Escape") {
        setActiveNav("overview");
        setMobileDrawerOpen(false);
        return;
      }

      // 1–8 → navigate
      if (NAV_KEYS[e.key]) {
        e.preventDefault();
        setActiveNav(NAV_KEYS[e.key]);
        setMobileDrawerOpen(false);
        return;
      }

      // Cmd/Ctrl+K → command palette (reserved, noop for now)
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        return;
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, []);

  const contextValue = { activeNav, setActiveNav: handleNavigate };

  return (
    <AppShellCtx.Provider value={contextValue}>
      <ToastProvider>
        <div className="app-shell">
          <TopBar
            daemonStatus={daemonStatus}
            swarmPhase={swarmPhase}
            totalCost={totalCost}
            currentView={NAV_LABELS[activeNav] ?? "Overview"}
            onMenuToggle={handleToggleMobileDrawer}
          />

          <div className="app-shell__body">
            {/* Mobile backdrop */}
            {mobileDrawerOpen && (
              <div
                className="mobile-backdrop"
                onClick={() => setMobileDrawerOpen(false)}
              />
            )}

            <Sidebar
              activeItem={activeNav}
              onNavigate={handleNavigate}
              collapsed={sidebarCollapsed}
              onToggleCollapse={handleToggleCollapse}
              mobileOpen={mobileDrawerOpen}
            />

            <main
              id="main-content"
              className="app-shell__main"
            >
              {children}
            </main>
          </div>
        </div>
      </ToastProvider>
    </AppShellCtx.Provider>
  );
}
