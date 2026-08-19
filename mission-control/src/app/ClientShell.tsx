"use client";

import React, { useState, useCallback, useEffect } from "react";
import { useRouter, usePathname } from "next/navigation";
import { ToastProvider } from "@/components/ui/Toast";
import { PendingTaskProvider } from "@/contexts/PendingTaskContext";
import { TopBar } from "@/components/layout/TopBar";
import { TaskSidebar } from "@/components/ui/TaskSidebar";
import { useSystemStream } from "@/hooks/useSystemStream";
import { useTaskHistory } from "@/hooks/useTaskHistory";
import type { TaskHistoryFilters } from "@/hooks/useTaskHistory";

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
  if (pathname === "/agents" || pathname === "/skills") return "Agents";
  if (pathname === "/sessions") return "Hermes Sessions";
  if (pathname === "/settings") return "Settings";
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
  const [taskFilters, setTaskFilters] = useState<TaskHistoryFilters>({
    search: "",
    status: "",
    dateFrom: "",
    minCost: "",
    maxCost: "",
  });
  // ── System health (replaces useBlackboard.startPolling) ───────────
  const system = useSystemStream();

  // ── Task history (feeds sidebar and landing page stats) ───────────
  const taskHistory = useTaskHistory({
    ...taskFilters,
    status: taskFilters.status === "attention" ? "" : taskFilters.status,
  });

  // ── Re-fetch task list when system stream emits lifecycle events ──
  useEffect(() => {
    if (system.eventSequence > 0) {
      void taskHistory.refetch();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [system.eventSequence]);

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

  // ── Dynamic Tab Title (running tasks) ─────────────────────────────
  const runningTasks = taskHistory.tasks.filter((t) => t.status === "running").length;
  useEffect(() => {
    const defaultTitle = "bMAS — Mission Control";
    if (runningTasks > 0) {
      document.title = `(${runningTasks}) ${defaultTitle}`;
    } else {
      document.title = defaultTitle;
    }
  }, [runningTasks]);

  // ── Register PWA Service Worker ───────────────────────────────────
  useEffect(() => {
    if (!("serviceWorker" in navigator)) return;

    if (process.env.NODE_ENV !== "production") {
      void navigator.serviceWorker.getRegistrations().then((registrations) =>
        Promise.all(registrations.map((registration) => registration.unregister()))
      );
      if ("caches" in window) {
        void window.caches.keys().then((keys) =>
          Promise.all(
            keys
              .filter((key) => key.startsWith("bmas-swarm-cache-"))
              .map((key) => window.caches.delete(key))
          )
        );
      }
      return;
    }

    void navigator.serviceWorker.register("/sw.js").catch((error: unknown) => {
      console.error("bMAS service worker registration failed:", error);
    });
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
            systemState={system.connectionState}
            lastSuccessfulEventAt={system.lastSuccessfulEventAt}
            systemStateStale={system.isStale}
            failedDependencies={system.failedDependencies}
            affectedFeatures={system.affectedFeatures}
            onSystemRetry={system.reconnect}
            swarmPhase={undefined}
            totalCost={totalCost}
            currentView={currentView}
            onMenuToggle={handleToggleMobileDrawer}
          />

          {system.connectionState === "offline" && (
            <div className="offline-banner">
              <span className="offline-banner__dot" />
              Offline. Live data and server actions are unavailable.
            </div>
          )}

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
              filters={taskFilters}
              onFiltersChange={setTaskFilters}
              error={taskHistory.error}
              onRetry={() => void taskHistory.refetch()}
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
