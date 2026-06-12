"use client";

/**
 * Mission Cockpit — the flagship Phase 5 view (doc 13).
 *
 * A dense, multi-panel command center with four synchronized regions:
 *   1. Blackboard Graph (center) — live board visualization
 *   2. Agent Minds (right rail) — per-agent live cards
 *   3. Global Firehose (far right, toggleable) — all trace events
 *   4. Convergence Strip (bottom) — sparklines over rounds
 *
 * Cross-linking: clicking graph nodes highlights agent cards,
 * clicking agent cards opens TurnInspector.
 */

import { useMemo, useState, useCallback } from "react";
import { useTaskData } from "../TaskStreamContext";
import { BlackboardGraph } from "@/components/features/BlackboardGraph";
import { AgentMindCard } from "@/components/features/AgentMindCard";
import { GlobalFirehose } from "@/components/features/GlobalFirehose";
import { ConvergenceStrip } from "@/components/features/ConvergenceStrip";
import { BudgetGauge } from "@/components/features/BudgetGauge";
import { ConsensusMeter } from "@/components/features/ConsensusMeter";
import { TurnInspector } from "@/components/features/TurnInspector";
import { Panel } from "@/components/ui/Panel";
import {
  Eye,
  EyeOff,
  Pause,
  Play,
  MessageSquarePlus,
  Radio,
} from "lucide-react";

const DAEMON_URL = process.env.NEXT_PUBLIC_DAEMON_URL ?? "http://192.168.4.240:9000";

export default function MissionPage() {
  const {
    taskMeta,
    boardEntries,
    activeTurns,
    completedTurns,
    traceEvents,
    approvalRequests,
    isPaused,
    budgetState,
    coordinatorNarrations,
    isLive,
    consensus,
  } = useTaskData();

  const [showFirehose, setShowFirehose] = useState(true);
  const [selectedActor, setSelectedActor] = useState<string | null>(null);
  const [showDirectiveInput, setShowDirectiveInput] = useState(false);
  const [directiveText, setDirectiveText] = useState("");

  const taskId = taskMeta?.task_id ?? "";

  // Unique actors from board entries and turns
  const actors = useMemo(() => {
    const set = new Set<string>();
    for (const entry of boardEntries) {
      if (entry.author) set.add(entry.author);
    }
    for (const turn of [...activeTurns, ...completedTurns]) {
      if (turn.actor) set.add(turn.actor);
    }
    // Remove system actors
    set.delete("control_unit");
    set.delete("operator");
    return Array.from(set).sort();
  }, [boardEntries, activeTurns, completedTurns]);

  // Selected actor's latest turn for TurnInspector
  const selectedTurn = useMemo(() => {
    if (!selectedActor) return null;
    const actorTurns = [...activeTurns, ...completedTurns].filter(
      (t) => t.actor === selectedActor,
    );
    return actorTurns[actorTurns.length - 1] ?? null;
  }, [selectedActor, activeTurns, completedTurns]);

  // HITL: Pause / Resume
  const handleTogglePause = useCallback(async () => {
    const endpoint = isPaused ? "resume" : "pause";
    try {
      await fetch(`${DAEMON_URL}/api/tasks/${taskId}/${endpoint}`, {
        method: "POST",
      });
    } catch (e) {
      console.error("Pause/resume failed:", e);
    }
  }, [taskId, isPaused]);

  // HITL: Inject directive
  const handleDirective = useCallback(async () => {
    const trimmed = directiveText.trim();
    if (!trimmed) return;
    try {
      await fetch(`${DAEMON_URL}/api/tasks/${taskId}/directive`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ body: trimmed }),
      });
      setDirectiveText("");
      setShowDirectiveInput(false);
    } catch (e) {
      console.error("Directive injection failed:", e);
    }
  }, [taskId, directiveText]);

  return (
    <div className="mission-cockpit">
      {/* ── Top Bar ────────────────────────────────────────────────── */}
      <div className="mission-cockpit__topbar">
        <div className="mission-cockpit__topbar-left">
          <Radio
            size={14}
            style={{
              color: isLive ? "hsl(142, 71%, 45%)" : "hsl(220, 15%, 50%)",
              animation: isLive ? "pulse 2s infinite" : "none",
            }}
          />
          <span className="mission-cockpit__status">
            {isLive ? "Live" : "Replay"}
          </span>
          {isPaused && (
            <span className="mission-cockpit__paused-chip">⏸ Paused</span>
          )}
          {coordinatorNarrations.length > 0 && (
            <span className="mission-cockpit__narration">
              R{coordinatorNarrations[coordinatorNarrations.length - 1].round}:{" "}
              {coordinatorNarrations[coordinatorNarrations.length - 1].rationale?.slice(0, 80) ?? "Selecting agents…"}
            </span>
          )}
        </div>

        <div className="mission-cockpit__topbar-right">
          {budgetState && (
            <BudgetGauge
              spent={budgetState.spent}
              ceiling={budgetState.ceiling}
              size={48}
              strokeWidth={4}
              compact
            />
          )}
          {consensus && (
            <ConsensusMeter value={consensus.convergence ?? 0} size={48} />
          )}

          {/* HITL controls */}
          {isLive && (
            <>
              <button
                className="mission-cockpit__btn"
                onClick={handleTogglePause}
                title={isPaused ? "Resume" : "Pause at round boundary"}
              >
                {isPaused ? <Play size={14} /> : <Pause size={14} />}
              </button>
              <button
                className="mission-cockpit__btn"
                onClick={() => setShowDirectiveInput(!showDirectiveInput)}
                title="Inject directive"
              >
                <MessageSquarePlus size={14} />
              </button>
              <button
                className="mission-cockpit__btn"
                onClick={() => setShowFirehose(!showFirehose)}
                title={showFirehose ? "Hide firehose" : "Show firehose"}
              >
                {showFirehose ? <EyeOff size={14} /> : <Eye size={14} />}
              </button>
            </>
          )}
        </div>
      </div>

      {/* Directive input bar */}
      {showDirectiveInput && (
        <div className="mission-cockpit__directive-bar">
          <input
            type="text"
            className="mission-cockpit__directive-input"
            placeholder="Type a directive for the swarm…"
            value={directiveText}
            onChange={(e) => setDirectiveText(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && handleDirective()}
            maxLength={2000}
            autoFocus
          />
          <button
            className="mission-cockpit__directive-send"
            onClick={handleDirective}
            disabled={!directiveText.trim()}
          >
            Send
          </button>
        </div>
      )}

      {/* ── Main Content ──────────────────────────────────────────── */}
      <div
        className={`mission-cockpit__body ${showFirehose ? "mission-cockpit__body--with-firehose" : ""}`}
      >
        {/* Center: Blackboard Graph */}
        <div className="mission-cockpit__center">
          <Panel title="Blackboard" compact>
            <BlackboardGraph
              entries={boardEntries}
              compact
            />
          </Panel>
        </div>

        {/* Right Rail: Agent Minds */}
        <div className="mission-cockpit__agents">
          <div className="mission-cockpit__agents-header">
            <span>Agent Minds</span>
            <span className="mission-cockpit__agents-count">
              {actors.length}
            </span>
          </div>
          <div className="mission-cockpit__agents-list">
            {actors.map((actor) => (
              <AgentMindCard
                key={actor}
                actor={actor}
                traceEvents={traceEvents}
                activeTurns={activeTurns}
                approvalRequests={approvalRequests}
                onClick={() =>
                  setSelectedActor(
                    selectedActor === actor ? null : actor,
                  )
                }
              />
            ))}
          </div>
        </div>

        {/* Far Right: Global Firehose */}
        {showFirehose && (
          <div className="mission-cockpit__firehose">
            <GlobalFirehose events={traceEvents} />
          </div>
        )}
      </div>

      {/* ── Bottom: Convergence Strip ─────────────────────────────── */}
      <ConvergenceStrip
        entries={boardEntries}
        budgetState={budgetState}
        narrations={coordinatorNarrations}
      />

      {/* ── Turn Inspector Modal ──────────────────────────────────── */}
      {selectedTurn && (
        <div className="mission-cockpit__inspector-overlay">
          <div className="mission-cockpit__inspector-panel">
            <button
              className="mission-cockpit__inspector-close"
              onClick={() => setSelectedActor(null)}
            >
              ✕
            </button>
            <TurnInspector
              turn={selectedTurn}
              traceEvents={traceEvents.filter(
                (t) => t.turn_id === selectedTurn.turn_id,
              )}
            />
          </div>
        </div>
      )}
    </div>
  );
}
