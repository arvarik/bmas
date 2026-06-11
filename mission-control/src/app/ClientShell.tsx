"use client";

import React, { useState, useCallback, useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { ToastProvider } from "@/components/ui/Toast";
import { PendingTaskProvider } from "@/contexts/PendingTaskContext";
import { TopBar } from "@/components/layout/TopBar";
import { TaskSidebar } from "@/components/ui/TaskSidebar";
import { useSystemStream } from "@/hooks/useSystemStream";
import { useTaskHistory } from "@/hooks/useTaskHistory";
import type { StatusType } from "@/lib/design-tokens";

// ── Route → breadcrumb label mapping ─────────────────────────────────

function getBreadcrumb(pathname: string): string {
  if (pathname === "/") return "Home";
  if (pathname.startsWith("/task/")) {
    const segments = pathname.split("/");
    const taskId = segments[2] ?? "";
    const tab = segments[3];
    const tabLabel = tab
      ? tab.charAt(0).toUpperCase() + tab.slice(1)
      : "Overview";
    return `Task ${taskId.slice(0, 8)} / ${tabLabel}`;
  }
  if (pathname === "/infra") return "Infrastructure";
  if (pathname === "/skills") return "Skills";
  return "Overview";
}

/**
 * ClientShell — client-side layout wrapper.
 *
 * Manages sidebar collapsed state, mobile drawer, TopBar rendering,
 * Toast + PendingTask providers, keyboard shortcuts, and responsive
 * sidebar behavior.
 *
 * Data sources (Phase 6):
 * - useSystemStream() — SSE for daemon/agent health + task lifecycle
 * - useTaskHistory() — REST for task list (sidebar + landing page)
 *
 * Navigation is URL-based via Next.js App Router. TaskSidebar uses
 * native <Link> components — no imperative route mapping needed.
 */
export function ClientShell({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();

  const [sidebarCollapsed, setSidebarCollapsed] = useState(false);
  const [mobileDrawerOpen, setMobileDrawerOpen] = useState(false);

  // ── System health (replaces useBlackboard.startPolling) ───────────
  const system = useSystemStream();

  // ── Task history (feeds sidebar and landing page stats) ───────────
  const taskHistory = useTaskHistory();

  // ── Re-fetch task list when system stream emits lifecycle events ──
  useEffect(() => {
    if (system.eventSequence > 0) {
      void taskHistory.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [system.eventSequence]);

  // Map daemon status to TopBar's StatusType
  const daemonStatus: StatusType =
    system.daemonStatus === "healthy"
      ? "running"
      : system.daemonStatus === "degraded"
        ? "paused"
        : "pending";

  // Compute total cost from task history
  const totalCost = taskHistory.tasks.reduce(
    (sum, t) => sum + (t.total_cost_usd ?? 0),
    0
  );

  const handleToggleCollapse = useCallback(() => {
    setSidebarCollapsed((prev) => !prev);
  }, []);

  const handleToggleMobileDrawer = useCallback(() => {
    setMobileDrawerOpen((prev) => !prev);
  }, []);

  // ── Close mobile drawer on navigation ─────────────────────────────
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- sync UI state to pathname change
    setMobileDrawerOpen(false);
  }, [pathname]);

  // ── Responsive: auto-collapse sidebar at <1024px ──────────────────
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

  // ── Global keyboard shortcuts ─────────────────────────────────────
  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      const tag = (e.target as HTMLElement)?.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      // Esc → back to landing page
      if (e.key === "Escape") {
        router.push("/");
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
  }, [router]);

  const currentView = getBreadcrumb(pathname);

  return (
    <ToastProvider>
      <PendingTaskProvider>
        <div className="app-shell">
          <TopBar
            daemonStatus={daemonStatus}
            swarmPhase={undefined}
            totalCost={totalCost}
            currentView={currentView}
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

            <TaskSidebar
              tasks={taskHistory.tasks}
              agentHealth={system.agentHealth}
              collapsed={sidebarCollapsed}
              onToggleCollapse={handleToggleCollapse}
              mobileOpen={mobileDrawerOpen}
              isLoading={taskHistory.isLoading}
              hasMore={taskHistory.hasMore}
              onLoadMore={taskHistory.loadMore}
            />

            <main id="main-content" className="app-shell__main">
              {children}
            </main>
          </div>
        </div>
      </PendingTaskProvider>
    </ToastProvider>
  );
}
