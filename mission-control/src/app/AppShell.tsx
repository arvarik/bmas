"use client";

import React, { useState, useCallback, useEffect } from "react";
import { ToastProvider } from "@/components/ui/Toast";
import { TopBar } from "@/components/TopBar";
import { Sidebar } from "@/components/ui/Sidebar";
import { useBlackboard } from "@/hooks/useBlackboard";

/**
 * AppShell — client-side layout wrapper.
 *
 * Manages sidebar collapsed state, active navigation item, TopBar
 * rendering, Toast provider, keyboard shortcuts, and responsive
 * sidebar behavior.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [activeNav, setActiveNav] = useState("dashboard");

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

  const daemonStatus = daemonError
    ? "error" as const
    : daemonState
      ? "running" as const
      : "pending" as const;

  const swarmPhase = daemonState?.phase
    ? `${daemonState.phase} — Iteration ${daemonState.iteration}`
    : undefined;

  // Compute total cost from the store (if available)
  // This will be wired to the cost API in a future phase; for now, placeholder
  const totalCost = 0;

  const handleToggleCollapse = useCallback(() => {
    setSidebarCollapsed((prev) => !prev);
  }, []);

  const handleNavigate = useCallback((id: string) => {
    setActiveNav((prev) => (prev === id ? "dashboard" : id));
  }, []);

  // ── Responsive: auto-collapse sidebar at <1440px ──────────────
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 1439px)");
    const handler = (e: MediaQueryListEvent | MediaQueryList) => {
      setSidebarCollapsed(e.matches);
    };
    handler(mq);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);

  // ── Global keyboard shortcuts ─────────────────────────────────
  useEffect(() => {
    const NAV_KEYS: Record<string, string> = {
      "1": "dag", "2": "logs", "3": "hitl",
      "4": "blackboard", "5": "cost", "6": "nodes",
    };

    function handleKeyDown(e: KeyboardEvent) {
      // Don't hijack when user is typing in an input
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      // Esc → back to dashboard
      if (e.key === "Escape") {
        setActiveNav("dashboard");
        return;
      }

      // 1–6 → navigate
      if (NAV_KEYS[e.key]) {
        e.preventDefault();
        setActiveNav((prev) => (prev === NAV_KEYS[e.key] ? "dashboard" : NAV_KEYS[e.key]));
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

  return (
    <ToastProvider>
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          height: "100vh",
          overflow: "hidden",
          background: "var(--surface-base)",
        }}
      >
        <TopBar
          daemonStatus={daemonStatus}
          swarmPhase={swarmPhase}
          totalCost={totalCost}
        />

        <div style={{ display: "flex", flex: 1, minHeight: 0, overflow: "hidden" }}>
          <Sidebar
            activeItem={activeNav}
            onNavigate={handleNavigate}
            collapsed={sidebarCollapsed}
            onToggleCollapse={handleToggleCollapse}
          />

          <main
            id="main-content"
            style={{
              flex: 1,
              minWidth: 0,
              overflow: "auto",
              padding: "var(--space-3)",
              background: "var(--surface-base)",
            }}
          >
            {/* Pass activeNav to children via a wrapper div with data attribute */}
            <div data-active-nav={activeNav} style={{ height: "100%" }}>
              {children}
            </div>
          </main>
        </div>
      </div>
    </ToastProvider>
  );
}
