"use client";

import dynamic from "next/dynamic";
import { useState } from "react";
import { Skeleton } from "@/components/ui/Skeleton";
import { AGENT_COLORS, type AgentRole } from "@/lib/design-tokens";

const LogTerminal = dynamic(() => import("@/components/LogTerminal"), {
  ssr: false,
  loading: () => (
    <div style={{ height: "100%", background: "var(--surface-overlay)", borderRadius: "var(--radius-lg)", display: "flex", alignItems: "center", justifyContent: "center" }}>
      <Skeleton variant="text" />
    </div>
  ),
});

const ROLES: { role: AgentRole; label: string }[] = [
  { role: "planner", label: "Planner" },
  { role: "executor", label: "Executor" },
  { role: "auditor", label: "Auditor" },
];

export default function LogsView() {
  const [activeTab, setActiveTab] = useState<AgentRole>("planner");

  return (
    <div className="view-container logs-view">
      {/* Mobile tab bar */}
      <div className="logs-tabs">
        {ROLES.map(({ role, label }) => {
          const isActive = activeTab === role;
          return (
            <button
              key={role}
              className={`logs-tab ${isActive ? "logs-tab--active" : ""}`}
              onClick={() => setActiveTab(role)}
              style={{
                borderBottomColor: isActive ? AGENT_COLORS[role] : "transparent",
                color: isActive ? AGENT_COLORS[role] : undefined,
              }}
            >
              <span
                className="logs-tab__dot"
                style={{ background: AGENT_COLORS[role] }}
              />
              {label}
            </button>
          );
        })}
      </div>

      {/* Mobile: single terminal */}
      <div className="logs-mobile-terminal">
        <LogTerminal role={activeTab} key={activeTab} />
      </div>

      {/* Desktop: all three side-by-side */}
      <div className="logs-desktop-grid">
        {ROLES.map(({ role }) => (
          <div key={role} className="logs-desktop-terminal">
            <LogTerminal role={role} />
          </div>
        ))}
      </div>
    </div>
  );
}
