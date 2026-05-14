"use client";

import React from "react";
import {
  LayoutDashboard,
  GitBranch,
  ScrollText,
  Joystick,
  Clipboard,
  DollarSign,
  Server,
  Sparkles,
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

// ── Navigation Items ─────────────────────────────────────────────────

interface NavItem {
  id: string;
  label: string;
  icon: LucideIcon;
}

interface NavSection {
  title: string;
  items: NavItem[];
}

const NAV_SECTIONS: NavSection[] = [
  {
    title: "",
    items: [
      { id: "overview", label: "Overview", icon: LayoutDashboard },
    ],
  },
  {
    title: "Operations",
    items: [
      { id: "dag", label: "DAG", icon: GitBranch },
      { id: "logs", label: "Logs", icon: ScrollText },
      { id: "operator", label: "Operator", icon: Joystick },
    ],
  },
  {
    title: "Intelligence",
    items: [
      { id: "blackboard", label: "Blackboard", icon: Clipboard },
      { id: "cost", label: "Cost", icon: DollarSign },
    ],
  },
  {
    title: "System",
    items: [
      { id: "infra", label: "Infrastructure", icon: Server },
      { id: "skills", label: "Skills", icon: Sparkles },
    ],
  },
];

// ── Agent Health Dots ────────────────────────────────────────────────

interface AgentDot {
  name: string;
  role: "planner" | "executor" | "auditor";
  cssVar: string;
}

const AGENT_DOTS: AgentDot[] = [
  { name: "Planner", role: "planner", cssVar: "var(--agent-planner)" },
  { name: "Executor", role: "executor", cssVar: "var(--agent-executor)" },
  { name: "Auditor", role: "auditor", cssVar: "var(--agent-auditor)" },
];

// ── Component ────────────────────────────────────────────────────────

export interface SidebarProps {
  activeItem: string;
  onNavigate: (id: string) => void;
  collapsed: boolean;
  onToggleCollapse: () => void;
  agentHealth?: Record<string, boolean>;
  mobileOpen?: boolean;
}

export function Sidebar({
  activeItem,
  onNavigate,
  collapsed,
  onToggleCollapse,
  agentHealth = { planner: true, executor: true, auditor: true },
  mobileOpen = false,
}: SidebarProps) {
  // Build class name for mobile/desktop states
  const sidebarClass = [
    "sidebar",
    collapsed ? "sidebar--collapsed" : "sidebar--expanded",
    mobileOpen ? "sidebar--mobile-open" : "",
  ].filter(Boolean).join(" ");

  return (
    <aside className={sidebarClass}>
      {/* ── Collapse Toggle ───────────────────────────────────────── */}
      <div className="sidebar__toggle-row">
        <button
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          className="sidebar__toggle-btn"
        >
          {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
        </button>
      </div>

      {/* ── Navigation ────────────────────────────────────────────── */}
      <nav className="sidebar__nav">
        {NAV_SECTIONS.map((section) => (
          <div key={section.title || "home"} className="sidebar__section">
            {/* Section Label */}
            {section.title && !collapsed && (
              <div className="sidebar__section-label">
                {section.title}
              </div>
            )}

            {/* Items */}
            <div className="sidebar__items">
              {section.items.map((item) => {
                const isActive = activeItem === item.id;
                const Icon = item.icon;
                return (
                  <button
                    key={item.id}
                    onClick={() => onNavigate(item.id)}
                    title={collapsed ? item.label : undefined}
                    aria-label={item.label}
                    aria-current={isActive ? "page" : undefined}
                    className={`sidebar__item ${isActive ? "sidebar__item--active" : ""}`}
                  >
                    {/* Active indicator bar */}
                    {isActive && <div className="sidebar__active-bar" />}
                    <Icon size={18} className="sidebar__item-icon" />
                    {!collapsed && <span className="sidebar__item-label">{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* ── Agent Health Dots ──────────────────────────────────────── */}
      <div className="sidebar__footer">
        {AGENT_DOTS.map((agent) => {
          const isHealthy = agentHealth[agent.role] ?? false;
          return (
            <div
              key={agent.role}
              title={`${agent.name}: ${isHealthy ? "Connected" : "Disconnected"}`}
              className="sidebar__agent-dot"
              style={{
                background: isHealthy
                  ? "var(--status-success)"
                  : "var(--status-error)",
              }}
            />
          );
        })}
      </div>
    </aside>
  );
}

export default Sidebar;
