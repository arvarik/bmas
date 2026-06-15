"use client";

/**
 * AgentMindCard — Per-agent live reasoning card for Mission cockpit.
 *
 * Shows the latest reasoning stream, current tool indicator,
 * token meter, cost chip, status (active/idle/pending approval),
 * board entries created, rounds participated, and model info.
 *
 * @module Phase 5 (doc 13 §2)
 */

import { useMemo } from "react";
import { authorColor, STATUS_COLORS } from "@/lib/design-tokens";
import type { TraceEvent, TurnRecord, ApprovalRequest, BoardEntry } from "@/hooks/useTaskStream";
import { Cpu, Pause, CheckCircle2, Circle, MessageSquare, Layers, Zap } from "lucide-react";

interface AgentMindCardProps {
  actor: string;
  traceEvents: TraceEvent[];
  activeTurns: TurnRecord[];
  completedTurns?: TurnRecord[];
  approvalRequests: ApprovalRequest[];
  boardEntries?: BoardEntry[];
  onClick?: () => void;
  compact?: boolean;
}

export function AgentMindCard({
  actor,
  traceEvents,
  activeTurns,
  completedTurns = [],
  approvalRequests,
  boardEntries = [],
  onClick,
  compact = false,
}: AgentMindCardProps) {
  const color = authorColor(actor);

  // Latest trace events for this actor
  const actorTraces = useMemo(
    () => traceEvents.filter((t) => t.actor === actor).slice(-20),
    [traceEvents, actor],
  );

  // Is this actor currently active?
  const isActive = activeTurns.some((t) => t.actor === actor);

  // Is there a pending approval?
  const pendingApproval = approvalRequests.find(
    (r) => r.actor === actor,
  );

  // Latest reasoning line — show more context
  const latestReasoning = useMemo(() => {
    for (let i = actorTraces.length - 1; i >= 0; i--) {
      const t = actorTraces[i];
      if (t.type === "reasoning" || t.type === "thinking") {
        return t.content?.slice(0, 400) ?? "";
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

  // Board entries by this actor
  const actorEntries = useMemo(
    () => boardEntries.filter((e) => e.author === actor),
    [boardEntries, actor],
  );

  // Entry type breakdown
  const entryTypeCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const e of actorEntries) {
      counts.set(e.type, (counts.get(e.type) ?? 0) + 1);
    }
    return counts;
  }, [actorEntries]);

  // Turns for this actor (completed)
  const actorCompletedTurns = useMemo(
    () => completedTurns.filter((t) => t.actor === actor),
    [completedTurns, actor],
  );

  // Rounds participated
  const rounds = useMemo(() => {
    const set = new Set<number>();
    for (const t of [...actorCompletedTurns, ...activeTurns.filter(t => t.actor === actor)]) {
      if (t.round_no > 0) set.add(t.round_no);
    }
    return set;
  }, [actorCompletedTurns, activeTurns, actor]);

  // Cost for this actor
  const actorCost = useMemo(() => {
    let total = 0;
    for (const t of actorCompletedTurns) {
      total += t.cost_usd ?? 0;
    }
    return total;
  }, [actorCompletedTurns]);

  // Model from latest turn
  const model = useMemo(() => {
    const turns = [...actorCompletedTurns, ...activeTurns.filter(t => t.actor === actor)];
    for (let i = turns.length - 1; i >= 0; i--) {
      if (turns[i].model) return turns[i].model;
    }
    return null;
  }, [actorCompletedTurns, activeTurns, actor]);

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
          {model && (
            <span className="agent-mind-card__model">{model}</span>
          )}
          {tokenCount > 0 && (
            <span className="agent-mind-card__tokens">
              {tokenCount.toLocaleString()} tok
            </span>
          )}
        </div>
      </div>

      {/* Stats row — board entries, rounds, cost */}
      <div className="agent-mind-card__stats">
        {actorEntries.length > 0 && (
          <span className="agent-mind-card__stat" title="Board entries created by this agent">
            <MessageSquare size={10} />
            <span>{actorEntries.length} {actorEntries.length === 1 ? "entry" : "entries"}</span>
            {entryTypeCounts.size > 0 && (
              <span className="agent-mind-card__stat-breakdown">
                ({[...entryTypeCounts.entries()]
                  .map(([type, count]) => `${count} ${type}`)
                  .join(", ")})
              </span>
            )}
          </span>
        )}
        {rounds.size > 0 && (
          <span className="agent-mind-card__stat" title="Rounds this agent participated in">
            <Layers size={10} />
            <span>{rounds.size === 1 ? `R${[...rounds][0]}` : `R${Math.min(...rounds)}–${Math.max(...rounds)}`}</span>
            <span className="agent-mind-card__stat-detail">
              · {actorCompletedTurns.length + (isActive ? 1 : 0)} turn{actorCompletedTurns.length + (isActive ? 1 : 0) !== 1 ? "s" : ""}
            </span>
          </span>
        )}
        {actorCost > 0 && (
          <span className="agent-mind-card__stat" title="Cost for this agent">
            <Zap size={10} />
            <span>${actorCost.toFixed(4)}</span>
          </span>
        )}
      </div>

      {/* Reasoning stream — expanded to 4 lines */}
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
