"use client";

/**
 * Blackboard Tab — /task/[taskId]/blackboard
 *
 * Phase 4: Graph/Stream segmented control (doc 08 §2).
 *
 * - **Graph** (new default): BlackboardGraph + WorkerLane + ConsensusMeter
 * - **Stream**: the existing debate list (preserved as-is)
 *
 * Debate history with:
 * - Agent-identity-colored entries with timestamps
 * - Phase-aware typing indicator (e.g. "Planner is deliberating…")
 * - IntersectionObserver-based smart auto-scroll with "N new updates" pill
 * - REST fallback for completed tasks (fetches from /api/tasks/{id}/debate)
 *
 */

import { useRef, useState, useEffect, useCallback } from "react";
import { useParams } from "next/navigation";
import { useTaskData } from "../TaskStreamContext";
import { Panel } from "@/components/ui/Panel";
import { Clipboard } from "lucide-react";
import { AGENT_COLORS, type AgentRole, authorColor } from "@/lib/design-tokens";
import type { DebateEntry } from "@/hooks/useTaskStream";
import { BlackboardGraph } from "@/components/features/BlackboardGraph";
import { WorkerLane } from "@/components/features/WorkerLane";
import { ConsensusMeter } from "@/components/features/ConsensusMeter";

// ── Phase → agent/verb mapping ────────────────────────────────────────

const PHASE_MAP: Record<string, { agent: AgentRole; verb: string }> = {
  triage: { agent: "planner", verb: "classifying" },
  planning: { agent: "planner", verb: "deliberating" },
  executing: { agent: "executor", verb: "working" },
  auditing: { agent: "auditor", verb: "reviewing" },
  synthesis: { agent: "planner", verb: "synthesizing" },
  finalizing: { agent: "auditor", verb: "finalizing" },
};

function capitalize(s: string): string {
  return s.charAt(0).toUpperCase() + s.slice(1);
}

// ── Archived debate entry from REST ───────────────────────────────────

interface ArchivedDebate {
  id: number;
  agent_role: string;
  content: string;
  created_at: string;
}

/** Map daemon debate shape → frontend DebateEntry. */
function mapArchivedDebate(raw: ArchivedDebate): DebateEntry {
  return {
    id: String(raw.id),
    agent_role: raw.agent_role ?? "unknown",
    content: raw.content ?? "",
    timestamp: raw.created_at ?? new Date().toISOString(),
  };
}

// ── View mode type ────────────────────────────────────────────────────

type ViewMode = "graph" | "stream";

// ── Component ─────────────────────────────────────────────────────────

export default function BlackboardPage() {
  const { taskId } = useParams();
  const {
    debates: liveDebates,
    phase,
    isLive,
    boardEntries,
    removedEntryIds,
    consensus,
    activeTurns,
    completedTurns,
    traceEvents,
    taskMeta,
  } = useTaskData();

  const [viewMode, setViewMode] = useState<ViewMode>("graph");

  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);
  const [isNearBottom, setIsNearBottom] = useState(true);
  const [newUpdateCount, setNewUpdateCount] = useState(0);
  const prevLengthRef = useRef(liveDebates.length);

  // ── REST fallback for completed tasks ─────────────────────────────
  const [archivedDebates, setArchivedDebates] = useState<DebateEntry[]>([]);
  const [archiveLoading, setArchiveLoading] = useState(false);

  const fetchArchived = useCallback(async () => {
    if (isLive || !taskId) return;
    setArchiveLoading(true);
    try {
      const res = await fetch(`/api/tasks/${taskId}/debate`);
      if (res.ok) {
        const data = await res.json();
        const entries: ArchivedDebate[] = Array.isArray(data) ? data : data.entries ?? [];
        setArchivedDebates(entries.map(mapArchivedDebate));
      }
    } catch {
      // Archived debates are best-effort
    } finally {
      setArchiveLoading(false);
    }
  }, [isLive, taskId]);

  useEffect(() => {
    if (!isLive && liveDebates.length === 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch, setState is in the callback
      void fetchArchived();
    }
  }, [isLive, liveDebates.length, fetchArchived]);

  // Use live debates when streaming, archived when completed
  const debates = liveDebates.length > 0 ? liveDebates : archivedDebates;

  // ── IntersectionObserver for smart auto-scroll ────────────────────
  useEffect(() => {
    const sentinel = sentinelRef.current;
    if (!sentinel) return;

    const observer = new IntersectionObserver(
      ([entry]) => {
        setIsNearBottom(entry.isIntersecting);
        if (entry.isIntersecting) setNewUpdateCount(0);
      },
      { rootMargin: "50px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, []);

  // ── Track new entries arriving while scrolled up ──────────────────
  useEffect(() => {
    const newEntries = debates.length - prevLengthRef.current;
    prevLengthRef.current = debates.length;

    if (newEntries > 0) {
      if (isNearBottom) {
        // Auto-scroll
        sentinelRef.current?.scrollIntoView({ behavior: "smooth" });
      } else {
        setNewUpdateCount((prev) => prev + newEntries);
      }
    }
  }, [debates.length, isNearBottom]);

  const handleSnapToBottom = useCallback(() => {
    sentinelRef.current?.scrollIntoView({ behavior: "smooth" });
    setNewUpdateCount(0);
  }, []);

  // ── Typing indicator state ────────────────────────────────────────
  const phaseInfo = phase ? PHASE_MAP[phase] : null;

  // ── Loading state (archived fetch in progress) ────────────────────
  if (archiveLoading && debates.length === 0 && boardEntries.length === 0) {
    return (
      <div className="view-container">
        <Panel title="Blackboard" status="loading" />
      </div>
    );
  }

  // ── Empty state ───────────────────────────────────────────────────

  if (debates.length === 0 && boardEntries.length === 0 && !isLive) {
    return (
      <div className="view-container">
        <Panel
          title="Blackboard"
          status="empty"
          emptyIcon={Clipboard}
          emptyMessage="No blackboard data"
          emptyHint="This task has no recorded board entries."
        />
      </div>
    );
  }

  return (
    <div className="view-container">
      <Panel
        title="Blackboard"
        subtitle={
          viewMode === "graph"
            ? `${boardEntries.length} entries`
            : `${debates.length} entries`
        }
        headerExtra={
          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
            <ConsensusMeter consensus={consensus} isLive={isLive} />
            {/* Graph/Stream segmented control */}
            <div
              className="bb-toggle"
              role="tablist"
              style={{
                display: "inline-flex",
                background: "var(--surface-hover)",
                borderRadius: "var(--radius-sm)",
                padding: 2,
              }}
            >
              <button
                role="tab"
                aria-selected={viewMode === "graph"}
                className={viewMode === "graph" ? "bb-toggle__tab bb-toggle__tab--active" : "bb-toggle__tab"}
                onClick={() => setViewMode("graph")}
                style={{
                  padding: "2px 10px",
                  fontSize: "var(--text-xs)",
                  fontWeight: "var(--weight-medium)",
                  borderRadius: "var(--radius-sm)",
                  border: "none",
                  cursor: "pointer",
                  fontFamily: "var(--font-sans)",
                  background: viewMode === "graph" ? "var(--surface-overlay)" : "transparent",
                  color: viewMode === "graph" ? "var(--text-primary)" : "var(--text-tertiary)",
                  transition: "background 150ms ease, color 150ms ease",
                }}
              >
                Graph
              </button>
              <button
                role="tab"
                aria-selected={viewMode === "stream"}
                className={viewMode === "stream" ? "bb-toggle__tab bb-toggle__tab--active" : "bb-toggle__tab"}
                onClick={() => setViewMode("stream")}
                style={{
                  padding: "2px 10px",
                  fontSize: "var(--text-xs)",
                  fontWeight: "var(--weight-medium)",
                  borderRadius: "var(--radius-sm)",
                  border: "none",
                  cursor: "pointer",
                  fontFamily: "var(--font-sans)",
                  background: viewMode === "stream" ? "var(--surface-overlay)" : "transparent",
                  color: viewMode === "stream" ? "var(--text-primary)" : "var(--text-tertiary)",
                  transition: "background 150ms ease, color 150ms ease",
                }}
              >
                Stream
              </button>
            </div>
          </div>
        }
      >
        {/* ── Graph Mode ───────────────────────────────────────────── */}
        {viewMode === "graph" && (
          <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
            <WorkerLane
              activeTurns={activeTurns}
              completedTurns={completedTurns}
              traceEvents={traceEvents}
              boardEntries={boardEntries}
            />
            <div style={{ flex: 1, minHeight: 300 }}>
              <BlackboardGraph
                entries={boardEntries}
                removedEntryIds={removedEntryIds}
                variant={taskMeta?.variant}
              />
            </div>
          </div>
        )}

        {/* ── Stream Mode (existing debate list) ───────────────────── */}
        {viewMode === "stream" && (
          <>
            <div className="debate-scroll" ref={scrollRef}>
              {debates.map((entry) => {
                const agentColor =
                  AGENT_COLORS[entry.agent_role as AgentRole] ??
                  authorColor(entry.agent_role);
                return (
                  <div
                    key={entry.id}
                    className="debate-entry"
                    style={{ borderLeftColor: agentColor }}
                  >
                    <div className="debate-entry__header">
                      <span
                        className="debate-entry__agent-dot"
                        style={{ background: agentColor }}
                      />
                      <span
                        className="debate-entry__agent-name"
                        style={{ color: agentColor }}
                      >
                        {capitalize(entry.agent_role)}
                      </span>
                      <span className="debate-entry__timestamp">
                        {new Date(entry.timestamp).toLocaleTimeString()}
                      </span>
                    </div>
                    <div className="debate-entry__content">
                      {entry.content}
                    </div>
                  </div>
                );
              })}

              {/* Typing indicator */}
              {isLive && phaseInfo && (
                <div className="debate-entry debate-entry--typing">
                  <span
                    className="debate-entry__agent-dot"
                    style={{ background: AGENT_COLORS[phaseInfo.agent] }}
                  />
                  <span
                    className="debate-entry__agent-name"
                    style={{ color: AGENT_COLORS[phaseInfo.agent] }}
                  >
                    {capitalize(phaseInfo.agent)}
                  </span>
                  <span className="debate-entry__typing-label">
                    is {phaseInfo.verb}
                  </span>
                  <span className="debate-entry__typing-dots" aria-hidden="true">
                    <span /><span /><span />
                  </span>
                </div>
              )}

              {/* Waiting state when live but no entries yet */}
              {debates.length === 0 && isLive && (
                <div className="debate-empty-live">
                  <Clipboard size={20} />
                  <span>Waiting for debate entries…</span>
                </div>
              )}

              {/* Scroll sentinel */}
              <div ref={sentinelRef} style={{ height: 1 }} />
            </div>

            {/* "New updates" pill */}
            {newUpdateCount > 0 && (
              <button className="new-output-pill" onClick={handleSnapToBottom}>
                ↓ {newUpdateCount} new update{newUpdateCount !== 1 ? "s" : ""}
              </button>
            )}
          </>
        )}
      </Panel>
    </div>
  );
}
