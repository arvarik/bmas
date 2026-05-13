"use client";

import React from "react";
import {
  GitBranch,
  ScrollText,
  Hand,
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
    title: "Operations",
    items: [
      { id: "dag", label: "DAG", icon: GitBranch },
      { id: "logs", label: "Logs", icon: ScrollText },
      { id: "hitl", label: "HITL", icon: Hand },
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
    title: "Infrastructure",
    items: [
      { id: "nodes", label: "Nodes", icon: Server },
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
}

export function Sidebar({
  activeItem,
  onNavigate,
  collapsed,
  onToggleCollapse,
  agentHealth = { planner: true, executor: true, auditor: true },
}: SidebarProps) {
  const sidebarWidth = collapsed
    ? "var(--sidebar-collapsed-width)"
    : "var(--sidebar-width)";

  return (
    <aside
      style={{
        width: sidebarWidth,
        minWidth: sidebarWidth,
        height: "100%",
        background: "var(--surface-raised)",
        display: "flex",
        flexDirection: "column",
        transition: "width 200ms ease, min-width 200ms ease",
        overflow: "hidden",
        flexShrink: 0,
      }}
    >
      {/* ── Collapse Toggle ───────────────────────────────────────── */}
      <div
        style={{
          display: "flex",
          justifyContent: collapsed ? "center" : "flex-end",
          padding: `var(--space-3) ${collapsed ? "0" : "var(--space-3)"}`,
          flexShrink: 0,
        }}
      >
        <button
          onClick={onToggleCollapse}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            width: 28,
            height: 28,
            borderRadius: "var(--radius-sm)",
            border: "none",
            background: "transparent",
            color: "var(--text-tertiary)",
            cursor: "pointer",
            transition: "background 150ms ease, color 150ms ease",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "var(--surface-hover)";
            e.currentTarget.style.color = "var(--text-secondary)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "transparent";
            e.currentTarget.style.color = "var(--text-tertiary)";
          }}
        >
          {collapsed ? <PanelLeftOpen size={16} /> : <PanelLeftClose size={16} />}
        </button>
      </div>

      {/* ── Navigation ────────────────────────────────────────────── */}
      <nav
        style={{
          flex: 1,
          overflowY: "auto",
          overflowX: "hidden",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-4)",
          padding: `0 ${collapsed ? "0" : "var(--space-3)"}`,
        }}
      >
        {NAV_SECTIONS.map((section) => (
          <div key={section.title}>
            {/* Section Label */}
            {!collapsed && (
              <div
                style={{
                  fontSize: "var(--text-xs)",
                  fontWeight: "var(--weight-medium)",
                  color: "var(--text-tertiary)",
                  letterSpacing: "0.1em",
                  textTransform: "uppercase",
                  padding: `var(--space-2) var(--space-3)`,
                }}
              >
                {section.title}
              </div>
            )}

            {/* Items */}
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 2,
              }}
            >
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
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "var(--space-3)",
                      height: 36,
                      padding: collapsed
                        ? "0"
                        : "0 var(--space-4)",
                      justifyContent: collapsed ? "center" : "flex-start",
                      borderRadius: "var(--radius-sm)",
                      border: "none",
                      background: isActive
                        ? "var(--surface-active)"
                        : "transparent",
                      color: isActive
                        ? "var(--text-primary)"
                        : "var(--text-secondary)",
                      cursor: "pointer",
                      fontSize: "var(--text-sm)",
                      fontWeight: isActive
                        ? "var(--weight-medium)"
                        : "var(--weight-regular)",
                      fontFamily: "var(--font-sans)",
                      position: "relative",
                      transition: "background 150ms ease, color 150ms ease",
                      width: "100%",
                      textAlign: "left",
                    }}
                    onMouseEnter={(e) => {
                      if (!isActive) {
                        e.currentTarget.style.background = "var(--surface-hover)";
                      }
                    }}
                    onMouseLeave={(e) => {
                      if (!isActive) {
                        e.currentTarget.style.background = "transparent";
                      }
                    }}
                  >
                    {/* Active indicator bar */}
                    {isActive && (
                      <div
                        style={{
                          position: "absolute",
                          left: 0,
                          top: "20%",
                          bottom: "20%",
                          width: 2,
                          borderRadius: 1,
                          background: "var(--accent-primary)",
                        }}
                      />
                    )}
                    <Icon size={20} style={{ flexShrink: 0 }} />
                    {!collapsed && <span>{item.label}</span>}
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {/* ── Agent Health Dots ──────────────────────────────────────── */}
      <div
        style={{
          padding: collapsed ? "var(--space-3) 0" : "var(--space-3)",
          display: "flex",
          justifyContent: "center",
          gap: "var(--space-3)",
          borderTop: "1px solid var(--border-default)",
          flexShrink: 0,
        }}
      >
        {AGENT_DOTS.map((agent) => {
          const isHealthy = agentHealth[agent.role] ?? false;
          return (
            <div
              key={agent.role}
              title={`${agent.name}: ${isHealthy ? "Connected" : "Disconnected"}`}
              style={{
                width: 8,
                height: 8,
                borderRadius: "var(--radius-full)",
                background: isHealthy
                  ? "var(--status-success)"
                  : "var(--status-error)",
                cursor: "default",
                transition: "background 300ms ease",
              }}
            />
          );
        })}
      </div>
    </aside>
  );
}

export default Sidebar;
