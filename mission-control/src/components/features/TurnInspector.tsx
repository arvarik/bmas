"use client";

/**
 * TurnInspector — right-side slide-over panel (doc 09 §4).
 *
 * Opens from board node click or worker card click.
 * Shows complete trace timeline for a single turn plus
 * "Resulted in" footer (board entries, rejections, artifacts).
 * Includes live approvals, run steering, and Hermes delegation details.
 */

import React, { useMemo, useState, useCallback } from "react";
import { authorColor } from "@/lib/design-tokens";
import type { TurnRecord, TraceEvent, BoardEntry, RejectedEntry } from "@/hooks/useTaskStream";
import { ToolCallCard } from "./ToolCallCard";
import { DelegationTree } from "./DelegationTree";
import { bodyPreview } from "./board/boardModel";
import { typeMeta } from "./board/boardModel";
import { X, CheckCircle, XCircle, Send } from "lucide-react";
import {
  requestTaskOperatorAction,
  useTaskOperatorAction,
} from "@/hooks/useTaskOperatorAction";

type ApprovalChoice = "once" | "session" | "always" | "deny";

// ── Trace line glyph map ──────────────────────────────────────────────

const TYPE_GLYPHS: Record<string, { glyph: string; color: string }> = {
  reasoning:        { glyph: "●", color: "var(--accent-primary)" },
  tool_call:        { glyph: "▸", color: "var(--status-paused)" },
  entries_posted:   { glyph: "◆", color: "var(--status-success)" },
  final:            { glyph: "✓", color: "var(--status-success)" },
  error:            { glyph: "✕", color: "var(--status-error)" },
  approval_request: { glyph: "⏸", color: "var(--status-paused)" },
  approval_response:{ glyph: "✓", color: "var(--status-success)" },
};

function getGlyph(type: string) {
  return TYPE_GLYPHS[type] ?? { glyph: "·", color: "var(--text-tertiary)" };
}

// ── Props ─────────────────────────────────────────────────────────────

interface TurnInspectorProps {
  turnId: string | null;
  activeTurns: TurnRecord[];
  completedTurns: TurnRecord[];
  traceEvents: TraceEvent[];
  boardEntries: BoardEntry[];
  rejectedEntries: RejectedEntry[];
  onClose: () => void;
}

// ── Component ─────────────────────────────────────────────────────────

export function TurnInspector({
  turnId,
  activeTurns,
  completedTurns,
  traceEvents,
  boardEntries,
  rejectedEntries,
  onClose,
}: TurnInspectorProps) {
  // Approval decision state: track which run_ids have been decided
  const [decidedRuns, setDecidedRuns] = useState<Record<string, string>>({});
  const [approvalPending, setApprovalPending] = useState<Record<string, boolean>>({});
  const [approvalErrors, setApprovalErrors] = useState<Record<string, string>>({});
  const [steerInput, setSteerInput] = useState("");
  const {
    state: steerAction,
    execute: executeSteer,
    retry: retrySteer,
  } = useTaskOperatorAction();
  const steerPending = steerAction.status === "pending";

  // Find turn
  const turn = useMemo(() => {
    if (!turnId) return null;
    return (
      activeTurns.find((t) => t.turn_id === turnId) ??
      completedTurns.find((t) => t.turn_id === turnId) ??
      null
    );
  }, [turnId, activeTurns, completedTurns]);

  // Filter traces for this turn
  const turnTraces = useMemo(() => {
    if (!turnId) return [];
    return traceEvents.filter((t) => t.turn_id === turnId);
  }, [turnId, traceEvents]);

  const runId = useMemo(() => {
    for (let index = turnTraces.length - 1; index >= 0; index -= 1) {
      const trace = turnTraces[index];
      if (trace.run_id) return trace.run_id;
      const dataRunId = trace.data?.run_id;
      if (typeof dataRunId === "string" && dataRunId) return dataRunId;
    }
    return null;
  }, [turnTraces]);
  const traceDecisions = useMemo(() => {
    const decisions: Record<string, string> = {};
    for (const trace of turnTraces) {
      if (trace.type !== "approval_response") continue;
      const responseRunId = trace.run_id ?? (
        typeof trace.data?.run_id === "string" ? trace.data.run_id : ""
      );
      const choice = typeof trace.data?.choice === "string" ? trace.data.choice : "responded";
      if (responseRunId) decisions[responseRunId] = choice;
    }
    return decisions;
  }, [turnTraces]);
  const isActiveTurn = activeTurns.some((item) => item.turn_id === turnId);

  // Board entries created during this turn (matched by actor + time window)
  const createdEntries = useMemo(() => {
    if (!turn) return [];
    return boardEntries.filter(
      (e) =>
        e.author === turn.actor &&
        new Date(e.created_at).getTime() >= new Date(turn.started_at).getTime(),
    );
  }, [turn, boardEntries]);

  // Rejections during this turn
  const turnRejections = useMemo(() => {
    if (!turn) return [];
    return rejectedEntries.filter((r) => r.actor === turn.actor);
  }, [turn, rejectedEntries]);

  // Send one approval choice to the active Hermes run.
  const handleApproval = useCallback(
    async (approvalRunId: string, choice: ApprovalChoice) => {
      const taskId = turn?.task_id;
      if (!taskId || !approvalRunId || approvalPending[approvalRunId]) return;
      setApprovalPending((current) => ({ ...current, [approvalRunId]: true }));
      setApprovalErrors((current) => ({ ...current, [approvalRunId]: "" }));
      try {
        await requestTaskOperatorAction({
          action: "approval",
          task_id: taskId,
          run_id: approvalRunId,
          choice,
        });
        setDecidedRuns((prev) => ({ ...prev, [approvalRunId]: choice }));
      } catch (error) {
        setApprovalErrors((current) => ({
          ...current,
          [approvalRunId]: error instanceof Error ? error.message : "Approval failed",
        }));
      } finally {
        setApprovalPending((current) => ({ ...current, [approvalRunId]: false }));
      }
    },
    [approvalPending, turn],
  );

  const steerRun = useCallback(async () => {
    const taskId = turn?.task_id;
    const input = steerInput.trim();
    if (!taskId || !runId || !input || steerPending) return;
    const result = await executeSteer(
      { action: "run-steer", task_id: taskId, run_id: runId, input },
      "Guidance accepted by Hermes.",
    );
    if (result.ok) {
      setSteerInput("");
    }
  }, [executeSteer, runId, steerInput, steerPending, turn]);

  const retryRunSteer = useCallback(async () => {
    const result = await retrySteer("Guidance accepted by Hermes.");
    if (result.ok) setSteerInput("");
  }, [retrySteer]);

  if (!turnId) return null;

  const actor = turn?.actor ?? turnTraces[0]?.actor ?? "unknown";
  const color = authorColor(actor);

  return (
    <>
      {/* Backdrop */}
      <div
        className="turn-inspector-backdrop"
        onClick={onClose}
        aria-hidden="true"
        style={{
          position: "fixed",
          inset: 0,
          background: "hsl(0 0% 0% / 0.4)",
          zIndex: 900,
          cursor: "pointer",
        }}
      />

      {/* Slide-over panel */}
      <div
        className="turn-inspector"
        role="dialog"
        aria-modal="true"
        aria-label={`${actor.replace(/_/g, " ")} turn details`}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          bottom: 0,
          width: "min(460px, 90vw)",
          background: "var(--surface-raised)",
          borderLeft: "1px solid var(--border-default)",
          zIndex: 901,
          display: "flex",
          flexDirection: "column",
          animation: "toast-enter 200ms ease-out",
          overflowY: "auto",
        }}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "var(--space-3)",
            padding: "var(--space-4)",
            borderBottom: "1px solid var(--border-subtle)",
            flexShrink: 0,
          }}
        >
          <span style={{ width: 10, height: 10, borderRadius: "var(--radius-full)", background: color, flexShrink: 0 }} />
          <div style={{ flex: 1, minWidth: 0 }}>
            <div style={{ fontWeight: "var(--weight-semibold)", fontSize: "var(--text-base)", textTransform: "capitalize" }}>
              {actor.replace(/_/g, " ")}
            </div>
            {turn && (
              <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)", display: "flex", gap: "var(--space-2)", flexWrap: "wrap" }}>
                <span>Round {turn.round_no}</span>
                <span>· {turn.phase}</span>
                <span>· {turn.status}</span>
                {turn.model && <span>· {turn.model}</span>}
              </div>
            )}
          </div>

          {/* Stats */}
          {turn && (turn.tokens_in != null || turn.cost_usd != null) && (
            <div style={{ fontSize: "var(--text-xs)", fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", color: "var(--text-tertiary)", textAlign: "right", flexShrink: 0 }}>
              {turn.tokens_in != null && (
                <div>{((turn.tokens_in ?? 0) + (turn.tokens_out ?? 0)).toLocaleString()} tok</div>
              )}
              {turn.cost_usd != null && <div>${turn.cost_usd.toFixed(4)}</div>}
            </div>
          )}

          <button
            type="button"
            onClick={onClose}
            aria-label="Close turn details"
            style={{
              background: "none",
              border: "none",
              cursor: "pointer",
              color: "var(--text-tertiary)",
              padding: "var(--space-1)",
              flexShrink: 0,
            }}
          >
            <X size={16} />
          </button>
        </div>

        {isActiveTurn && runId ? (
          <div className="hermes-run-steer">
            <div className="hermes-run-steer__label">
              <strong>Steer live Hermes run</strong>
              <code>{runId}</code>
            </div>
            <div className="hermes-run-steer__form">
              <input
                value={steerInput}
                onChange={(event) => setSteerInput(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    void steerRun();
                  }
                }}
                maxLength={2_000}
                placeholder="Give the active run new guidance"
                aria-label="Hermes run guidance"
              />
              <button
                type="button"
                onClick={() => void steerRun()}
                disabled={!steerInput.trim() || steerPending}
                aria-label="Send Hermes run guidance"
              >
                <Send size={13} /> {steerPending ? "Sending" : "Send"}
              </button>
            </div>
            {steerAction.status !== "idle" ? (
              <span
                className="hermes-run-steer__result"
                role={steerAction.status === "error" ? "alert" : "status"}
              >
                {steerAction.message}
                {steerAction.status === "error" ? (
                  <button
                    type="button"
                    onClick={() => void retryRunSteer()}
                    style={{ marginLeft: "var(--space-2)" }}
                  >
                    Retry
                  </button>
                ) : null}
              </span>
            ) : null}
          </div>
        ) : null}

        <DelegationTree traces={turnTraces} />

        {/* Trace timeline */}
        <div style={{ flex: 1, overflow: "auto", padding: "var(--space-3)" }}>
          {turnTraces.length === 0 ? (
            <div style={{ color: "var(--text-tertiary)", padding: "var(--space-4)" }}>
              <div style={{ textAlign: "center", marginBottom: "var(--space-3)", fontSize: "var(--text-sm)" }}>
                No granular trace data for this turn.
              </div>
              {turn && (
                <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
                  <div style={{ fontWeight: "var(--weight-semibold)", marginBottom: "var(--space-1)" }}>Turn Summary</div>
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    <div>Actor: <span style={{ textTransform: "capitalize" }}>{turn.actor.replace(/_/g, " ")}</span></div>
                    <div>Phase: {turn.phase}</div>
                    <div>Status: {turn.status}</div>
                    {turn.model && <div>Model: {turn.model}</div>}
                    {turn.started_at && <div>Started: {new Date(turn.started_at).toLocaleTimeString()}</div>}
                    {turn.ended_at && <div>Ended: {new Date(turn.ended_at).toLocaleTimeString()}</div>}
                    {(turn.tokens_in != null || turn.tokens_out != null) && (
                      <div>Tokens: {((turn.tokens_in ?? 0) + (turn.tokens_out ?? 0)).toLocaleString()}</div>
                    )}
                    {turn.cost_usd != null && <div>Cost: ${turn.cost_usd.toFixed(4)}</div>}
                  </div>
                </div>
              )}
              {/* CU routing rationale */}
              {turn?.rationale && (
                <div style={{
                  marginTop: "var(--space-3)",
                  padding: "var(--space-2) var(--space-3)",
                  borderRadius: "var(--radius-sm)",
                  background: "hsl(217 92% 55%/0.06)",
                  border: "1px solid hsl(217 92% 55%/0.12)",
                  fontSize: "var(--text-xs)",
                  color: "var(--text-secondary)",
                  lineHeight: "var(--leading-relaxed)",
                }}>
                  <div style={{ fontWeight: "var(--weight-semibold)", marginBottom: 2, color: "var(--accent-primary)", fontSize: 10, textTransform: "uppercase", letterSpacing: "0.04em" }}>Coordinator Rationale</div>
                  {turn.rationale}
                </div>
              )}
              {createdEntries.length > 0 && (
                <div style={{ marginTop: "var(--space-3)" }}>
                  <div style={{ fontWeight: "var(--weight-semibold)", fontSize: "var(--text-xs)", marginBottom: "var(--space-1)" }}>Board Entries Created</div>
                  {createdEntries.map((e) => {
                    const em = typeMeta(e.type);
                    const EIcon = em.icon;
                    return (
                      <div key={e.id} style={{ fontSize: "var(--text-xs)", padding: "6px 0", borderBottom: "1px solid var(--border-subtle)", display: "flex", flexDirection: "column", gap: 2 }}>
                        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                          <EIcon size={11} style={{ color: em.color, flexShrink: 0 }} />
                          <span style={{ fontWeight: "var(--weight-medium)", color: em.color }}>{em.label}</span>
                          {e.title && <span style={{ color: "var(--text-secondary)" }}> — {e.title}</span>}
                        </div>
                        <div style={{ color: "var(--text-tertiary)", lineHeight: "var(--leading-relaxed)", paddingLeft: 15 }}>
                          {bodyPreview(e.body, 180)}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ) : (
            turnTraces.map((trace) => {
              const { glyph, color: glyphColor } = getGlyph(trace.type);
              const isToolCall = trace.type === "tool_call";
              const isApproval = trace.type === "approval_request";

              return (
                <div
                  key={trace.id}
                  style={{
                    display: "flex",
                    alignItems: "flex-start",
                    gap: "var(--space-2)",
                    padding: "2px 0",
                    fontSize: "var(--text-sm)",
                  }}
                >
                  <span style={{ width: 14, textAlign: "center", color: glyphColor, fontFamily: "var(--font-mono)", fontSize: "var(--text-xs)", flexShrink: 0 }}>
                    {glyph}
                  </span>
                  <span style={{ fontSize: "var(--text-xs)", color: "var(--text-tertiary)", fontFamily: "var(--font-mono)", fontVariantNumeric: "tabular-nums", minWidth: 60, flexShrink: 0 }}>
                    {new Date(trace.timestamp).toLocaleTimeString()}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {isApproval ? (
                      <ApprovalInline
                        trace={trace}
                        decided={decidedRuns[trace.run_id ?? ""] ?? traceDecisions[trace.run_id ?? ""] ?? null}
                        pending={approvalPending[trace.run_id ?? ""] ?? false}
                        error={approvalErrors[trace.run_id ?? ""] ?? null}
                        onDecide={(decision) =>
                          handleApproval(trace.run_id ?? "", decision)
                        }
                      />
                    ) : isToolCall ? (
                      <ToolCallCard content={trace.content} />
                    ) : (
                      <span style={{ color: "var(--text-secondary)", wordBreak: "break-word" }}>
                        {trace.content}
                      </span>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* "Resulted in" footer */}
        {(createdEntries.length > 0 || turnRejections.length > 0) && (
          <div
            style={{
              borderTop: "1px solid var(--border-subtle)",
              padding: "var(--space-3)",
              flexShrink: 0,
            }}
          >
            <div style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-semibold)", color: "var(--text-tertiary)", marginBottom: "var(--space-2)" }}>
              Resulted in
            </div>

            {createdEntries.map((e) => (
              <div
                key={e.id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "var(--space-2)",
                  padding: "2px 0",
                  fontSize: "var(--text-xs)",
                  color: "var(--text-secondary)",
                }}
              >
                <span style={{ width: 6, height: 6, borderRadius: "var(--radius-full)", background: "var(--status-success)", flexShrink: 0 }} />
                <span style={{ fontWeight: "var(--weight-medium)" }}>{e.type}</span>
                <span style={{ color: "var(--text-tertiary)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {e.title || bodyPreview(e.body, 80)}
                </span>
              </div>
            ))}

            {turnRejections.map((r) => (
              <div
                key={r.entry_id}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "var(--space-2)",
                  padding: "2px 0",
                  fontSize: "var(--text-xs)",
                  color: "var(--status-error)",
                }}
              >
                <span style={{ width: 6, height: 6, borderRadius: "var(--radius-full)", background: "var(--status-error)", flexShrink: 0 }} />
                <span>Rejected: {r.reason}</span>
              </div>
            ))}
          </div>
        )}
      </div>
    </>
  );
}

// ── Approval Inline Component ─────────────────────────────────────────

function ApprovalInline({
  trace,
  decided,
  pending,
  error,
  onDecide,
}: {
  trace: TraceEvent;
  decided: string | null;
  pending: boolean;
  error: string | null;
  onDecide: (choice: ApprovalChoice) => void;
}) {
  const supportedChoices = new Set<ApprovalChoice>(["once", "session", "always", "deny"]);
  const advertisedChoices = Array.isArray(trace.data?.choices)
    ? trace.data.choices.filter((choice): choice is ApprovalChoice =>
        typeof choice === "string" && supportedChoices.has(choice as ApprovalChoice))
    : [];
  const choices: ApprovalChoice[] = advertisedChoices.length
    ? advertisedChoices
    : ["once", "session", "always", "deny"];
  const labels: Record<ApprovalChoice, string> = {
    once: "Allow once",
    session: "This session",
    always: "Always allow",
    deny: "Deny",
  };
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: "var(--space-1)",
        padding: "var(--space-2)",
        borderRadius: "var(--radius-sm)",
        background: "hsl(38 92% 50%/0.06)",
        border: "1px solid hsl(38 92% 50%/0.15)",
      }}
    >
      <div style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-semibold)", color: "var(--status-paused)" }}>
        ⏸ Approval Required
      </div>
      <div style={{ fontSize: "var(--text-xs)", color: "var(--text-secondary)" }}>
        {trace.content || "Agent is requesting approval to proceed."}
      </div>
      {decided ? (
        <div
          style={{
            fontSize: "var(--text-xs)",
            fontWeight: "var(--weight-semibold)",
            color: decided === "deny" ? "var(--status-error)" : "var(--status-success)",
          }}
        >
          {decided === "deny" ? "✕ Denied" : `✓ ${labels[decided as ApprovalChoice] ?? decided}`}
        </div>
      ) : (
        <div className="approval-choice-row">
          {choices.map((choice) => (
            <button
              key={choice}
              type="button"
              onClick={() => onDecide(choice)}
              disabled={pending}
              className={choice === "deny" ? "approval-choice--deny" : "approval-choice--allow"}
            >
              {choice === "deny" ? <XCircle size={12} /> : <CheckCircle size={12} />}
              {pending ? "Sending" : labels[choice]}
            </button>
          ))}
        </div>
      )}
      {error ? <div className="hermes-run-steer__result">{error}</div> : null}
    </div>
  );
}

export default TurnInspector;
