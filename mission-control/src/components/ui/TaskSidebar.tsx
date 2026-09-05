"use client";

import type { Ref } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import {
  BarChart3,
  Bot,
  ClipboardList,
  Database,
  FlaskConical,
  Gauge,
  GitCompareArrows,
  PanelLeftClose,
  PanelLeftOpen,
  Plus,
  Server,
  ShieldCheck,
  Settings,
  X, Ruler, FlaskRound, Scale } from "lucide-react";

export interface TaskSidebarProps {
  agentHealth: Record<string, { alive: boolean }>;
  attentionCount: number;
  runningCount?: number;
  collapsed: boolean;
  onToggleCollapse: () => void;
  mobileOpen: boolean;
  drawerMode: boolean;
  sidebarRef?: Ref<HTMLElement>;
  onRequestClose: () => void;
}

const GROUPS = [
  {
    label: "Work",
    links: [
      { href: "/", label: "New task", icon: Plus },
      { href: "/tasks", label: "Tasks", icon: ClipboardList, badge: true },
    ],
  },
  {
    label: "Evaluate",
    links: [
      { href: "/tests", label: "Tests", icon: FlaskConical },
      { href: "/runs", label: "Runs", icon: Gauge },
      { href: "/baselines", label: "Baselines", icon: GitCompareArrows },
      { href: "/runtimes", label: "Runtimes", icon: ShieldCheck },
      { href: "/datasets", label: "Datasets", icon: Database },
      { href: "/metrics", label: "Metrics", icon: Ruler },
      { href: "/studies", label: "Studies", icon: FlaskRound },
      { href: "/judges", label: "Judges", icon: Scale },
    ],
  },
  {
    label: "Observe",
    links: [
      { href: "/infra", label: "Operations", icon: Server },
      { href: "/agents", label: "Agents", icon: Bot },
      { href: "/analytics", label: "Analytics", icon: BarChart3 },
    ],
  },
  {
    label: "Configure",
    links: [{ href: "/settings", label: "Settings", icon: Settings }],
  },
] as const;

export function TaskSidebar({
  agentHealth,
  attentionCount,
  runningCount = 0,
  collapsed,
  onToggleCollapse,
  mobileOpen,
  drawerMode,
  sidebarRef,
  onRequestClose,
}: TaskSidebarProps) {
  const pathname = usePathname();
  const onlineAgents = Object.values(agentHealth).filter((agent) => agent.alive).length;
  const totalAgents = Object.keys(agentHealth).length;

  return (
    <aside
      id="primary-navigation"
      ref={sidebarRef}
      className={`sidebar task-sidebar ${collapsed ? "sidebar--collapsed" : "sidebar--expanded"} ${mobileOpen ? "sidebar--mobile-open" : ""}`}
      aria-label="Primary navigation"
      aria-hidden={drawerMode && !mobileOpen ? true : undefined}
      inert={drawerMode && !mobileOpen}
    >
      {drawerMode ? (
        <button type="button" className="task-sidebar__mobile-close" onClick={onRequestClose}>
          <X size={18} aria-hidden="true" />
          <span>Close navigation</span>
        </button>
      ) : null}

      <nav className="task-sidebar__nav" aria-label="Mission Control sections">
        {GROUPS.map((group) => (
          <div className="task-sidebar__group" key={group.label}>
            {!collapsed ? <p className="task-sidebar__group-label">{group.label}</p> : null}
            {group.links.map((item) => {
              const active = item.href === "/"
                ? pathname === "/"
                : pathname === item.href || pathname.startsWith(`${item.href}/`)
                  || (item.href === "/tasks" && pathname.startsWith("/task/"));
              const Icon = item.icon;
              const showAttention = "badge" in item && item.badge && attentionCount > 0;
              const showRunning = "badge" in item && item.badge && !showAttention && runningCount > 0;
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`task-sidebar__nav-link ${active ? "task-sidebar__nav-link--active" : ""}`}
                  aria-current={active ? "page" : undefined}
                  title={collapsed ? item.label : undefined}
                >
                  <Icon size={18} aria-hidden="true" />
                  {!collapsed ? <span>{item.label}</span> : null}
                  {showAttention ? (
                    <span className="task-sidebar__count" aria-label={`${attentionCount} tasks need attention`}>
                      {attentionCount > 99 ? "99+" : attentionCount}
                    </span>
                  ) : null}
                  {showRunning ? (
                    <span className="task-sidebar__running" aria-label={`${runningCount} tasks running`}>
                      <span className="status-dot status-dot--running pulse-dot" aria-hidden="true" />
                    </span>
                  ) : null}
                </Link>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="task-sidebar__footer">
        {!collapsed ? (
          <p className="task-sidebar__agent-summary">
            <span aria-hidden="true" className={onlineAgents > 0 ? "status-dot status-dot--success" : "status-dot status-dot--error"} />
            Agents online {onlineAgents}/{totalAgents || 0}
          </p>
        ) : null}
        {!drawerMode ? (
          <button
            type="button"
            className="task-sidebar__collapse-btn"
            onClick={onToggleCollapse}
            aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
          >
            {collapsed ? <PanelLeftOpen size={18} /> : <PanelLeftClose size={18} />}
            {!collapsed ? <span>Collapse</span> : null}
          </button>
        ) : null}
      </div>
    </aside>
  );
}
