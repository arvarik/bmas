"use client";

import { useEffect, useState, useCallback } from "react";
import { useBlackboard, selectTasks } from "@/hooks/useBlackboard";
import { Panel } from "@/components/ui/Panel";
import { ActionButton } from "@/components/ui/ActionButton";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useToast } from "@/hooks/useToast";
import { Hand } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface HitlState { paused: boolean; }

// ── Main Component ────────────────────────────────────────────────────

export default function HITLControls() {
  const [paused, setPaused] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [sending, setSending] = useState(false);
  const [selectedTask, setSelectedTask] = useState("");
  const [hintText, setHintText] = useState("");
  const [daemonError, setDaemonError] = useState<string | null>(null);

  const tasks = useBlackboard(selectTasks);
  const { toast } = useToast();

  // ── Poll pause state ────────────────────────────────────────────
  const fetchState = useCallback(async () => {
    try {
      const res = await fetch("/api/hitl", { cache: "no-store" });
      if (res.ok) {
        const data = (await res.json()) as HitlState;
        setPaused(data.paused);
        setDaemonError(null);
      } else {
        setDaemonError(`Daemon returned HTTP ${res.status}`);
      }
    } catch {
      setDaemonError("Daemon unreachable");
    }
  }, []);

  useEffect(() => {
    const timer = setTimeout(() => void fetchState(), 0);
    const id = setInterval(fetchState, 3_000);
    return () => { clearTimeout(timer); clearInterval(id); };
  }, [fetchState]);

  // ── Toggle pause ────────────────────────────────────────────────
  const togglePause = async () => {
    setToggling(true);
    try {
      const res = await fetch("/api/hitl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: paused ? "resume" : "pause" }),
      });
      if (res.ok) {
        const data = (await res.json()) as { paused: boolean };
        setPaused(data.paused);
        toast({ type: "success", message: data.paused ? "Swarm paused." : "Swarm resumed." });
      } else {
        toast({ type: "error", message: `Failed: HTTP ${res.status}` });
      }
    } catch (err) {
      toast({ type: "error", message: err instanceof Error ? err.message : "Network error" });
    } finally {
      setToggling(false);
    }
  };

  // ── Inject hint ─────────────────────────────────────────────────
  const injectHint = async () => {
    if (!selectedTask || !hintText.trim()) return;
    setSending(true);
    try {
      const res = await fetch("/api/hitl", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "inject-hint", task_id: selectedTask, hint_text: hintText.trim() }),
      });
      if (res.ok) {
        toast({ type: "success", message: `Hint delivered to ${selectedTask}` });
        setHintText("");
      } else {
        const body = (await res.json().catch(() => ({}))) as { error?: string };
        toast({ type: "error", message: body.error ?? `HTTP ${res.status}` });
      }
    } catch (err) {
      toast({ type: "error", message: err instanceof Error ? err.message : "Network error" });
    } finally {
      setSending(false);
    }
  };

  return (
    <Panel title="Swarm Control" emptyIcon={Hand} emptyMessage="No active session">
      <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-5)" }}>
        {/* Daemon error banner */}
        {daemonError && (
          <div style={{
            padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-sm)",
            background: "hsl(0,84%,60%,0.1)", borderLeft: "3px solid var(--status-error)",
            fontSize: "var(--text-sm)", color: "var(--status-error)",
          }}>
            ⚠ {daemonError}
          </div>
        )}

        {/* Pause / Resume */}
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--text-tertiary)" }}>
            Swarm State
          </span>

          <div style={{ display: "flex", alignItems: "center", gap: "var(--space-3)" }}>
            <ActionButton
              variant={paused ? "primary" : "danger"}
              loading={toggling}
              onClick={togglePause}
              style={{ flex: 1 }}
            >
              {paused ? "▶ Resume Swarm" : "⏸ Pause Swarm"}
            </ActionButton>
            <StatusBadge status={paused ? "paused" : "running"} />
          </div>
        </div>

        {/* Hint Injection */}
        <div style={{ display: "flex", flexDirection: "column", gap: "var(--space-3)" }}>
          <span style={{ fontSize: "var(--text-xs)", fontWeight: "var(--weight-medium)", textTransform: "uppercase", letterSpacing: "0.1em", color: "var(--text-tertiary)" }}>
            Inject Hint
          </span>

          <select
            id="hitl-task-selector"
            value={selectedTask}
            onChange={(e) => setSelectedTask(e.target.value)}
            style={{
              width: "100%", padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-md)",
              background: "var(--surface-hover)", border: "1px solid var(--border-default)",
              fontSize: "var(--text-sm)", color: "var(--text-primary)", fontFamily: "var(--font-sans)",
              outline: "none",
            }}
          >
            <option value="">Select task…</option>
            {tasks.map((t) => (<option key={t.id} value={t.id}>{t.label} ({t.status})</option>))}
          </select>

          <textarea
            id="hitl-hint-input"
            value={hintText}
            onChange={(e) => setHintText(e.target.value)}
            placeholder="Enter guidance for the swarm…"
            rows={3}
            style={{
              width: "100%", padding: "var(--space-2) var(--space-3)", borderRadius: "var(--radius-md)",
              background: "var(--surface-hover)", border: "1px solid var(--border-default)",
              fontSize: "var(--text-sm)", color: "var(--text-primary)", fontFamily: "var(--font-sans)",
              resize: "none", outline: "none", lineHeight: "var(--leading-sm)",
            }}
            onFocus={(e) => { e.currentTarget.style.borderColor = "var(--border-focus)"; }}
            onBlur={(e) => { e.currentTarget.style.borderColor = "var(--border-default)"; }}
          />

          <ActionButton
            variant="secondary"
            loading={sending}
            disabled={!selectedTask || !hintText.trim()}
            onClick={injectHint}
          >
            💡 Send Hint
          </ActionButton>
        </div>
      </div>
    </Panel>
  );
}
