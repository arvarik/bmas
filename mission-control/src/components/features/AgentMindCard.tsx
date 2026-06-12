"use client";

/**
 * AgentMindCard — Per-agent live reasoning card for Mission cockpit.
 *
 * Shows the latest reasoning stream, current tool indicator,
 * token meter, cost chip, and status (active/idle/pending approval).
 *
 * @module Phase 5 (doc 13 §2)
 */

import { useMemo } from "react";
import { authorColor, STATUS_COLORS } from "@/lib/design-tokens";
import type { TraceEvent, TurnRecord, ApprovalRequest } from "@/hooks/useTaskStream";
import { Cpu, Pause, CheckCircle2, Circle } from "lucide-react";

interface AgentMindCardProps {
  actor: string;
  traceEvents: TraceEvent[];
  activeTurns: TurnRecord[];
  approvalRequests: ApprovalRequest[];
  onClick?: () => void;
  compact?: boolean;
}

export function AgentMindCard({
  actor,
  traceEvents,
  activeTurns,
  approvalRequests,
  onClick,
  compact = false,
}: AgentMindCardProps) {
  const color = authorColor(actor);

  // Latest trace events for this actor
  const actorTraces = useMemo(
    () => traceEvents.filter((t) => t.actor === actor).slice(-10),
    [traceEvents, actor],
  );

  // Is this actor currently active?
  const isActive = activeTurns.some((t) => t.actor === actor);

  // Is there a pending approval?
  const pendingApproval = approvalRequests.find(
    (r) => r.actor === actor,
  );

  // Latest reasoning line
  const latestReasoning = useMemo(() => {
    for (let i = actorTraces.length - 1; i >= 0; i--) {
      const t = actorTraces[i];
      if (t.type === "reasoning" || t.type === "thinking") {
        return t.content?.slice(0, 200) ?? "";
      }
    }
    return "";
  }, [actorTraces]);

  // Current tool
  const currentTool = useMemo(() => {
    for (let i = actorTraces.length - 1; i >= 0; i--) {
      if (actorTraces[i].type === "tool_call") {
        return actorTraces[i].content?.split("\n")[0] ?? "tool";
      }
    }
    return null;
  }, [actorTraces]);

  // Token count from traces
  const tokenCount = useMemo(() => {
    let total = 0;
    for (const t of actorTraces) {
      if (t.type === "token_delta") {
        total += parseInt(t.content ?? "0", 10) || 0;
      }
    }
    return total;
  }, [actorTraces]);

  // Display name
  const displayName = actor.includes(".")
    ? actor.split(".")[1].replace(/_/g, " ")
    : actor;

  const statusColor = pendingApproval
    ? STATUS_COLORS.paused
    : isActive
      ? STATUS_COLORS.running
      : STATUS_COLORS.pending;

  const StatusIcon = pendingApproval
    ? Pause
    : isActive
      ? Cpu
      : tokenCount > 0
        ? CheckCircle2
        : Circle;

  if (compact) {
    return (
      <button
        className="agent-mind-pill"
        onClick={onClick}
        style={{
          borderColor: color,
          background: `linear-gradient(135deg, ${color}11, transparent)`,
        }}
        title={actor}
      >
        <span
          className="agent-mind-pill__dot"
          style={{ background: statusColor }}
        />
        <span className="agent-mind-pill__name">{displayName}</span>
      </button>
    );
  }

  return (
    <div
      className="agent-mind-card"
      onClick={onClick}
      style={{
        borderLeft: `3px solid ${color}`,
        cursor: onClick ? "pointer" : "default",
      }}
    >
      {/* Header */}
      <div className="agent-mind-card__header">
        <div className="agent-mind-card__identity">
          <StatusIcon
            size={12}
            style={{ color: statusColor, flexShrink: 0 }}
          />
          <span className="agent-mind-card__name" style={{ color }}>
            {displayName}
          </span>
        </div>
        <div className="agent-mind-card__badges">
          {tokenCount > 0 && (
            <span className="agent-mind-card__tokens">
              {tokenCount.toLocaleString()} tok
            </span>
          )}
        </div>
      </div>

      {/* Reasoning stream */}
      {latestReasoning && (
        <div className="agent-mind-card__reasoning">
          {latestReasoning}
        </div>
      )}

      {/* Tool indicator */}
      {currentTool && isActive && (
        <div className="agent-mind-card__tool">
          <Cpu size={10} style={{ color: STATUS_COLORS.running }} />
          <span>{currentTool}</span>
        </div>
      )}

      {/* Pending approval banner */}
      {pendingApproval && (
        <div className="agent-mind-card__approval">
          ⏸ Approval required: {pendingApproval.description || "Action pending"}
        </div>
      )}
    </div>
  );
}
