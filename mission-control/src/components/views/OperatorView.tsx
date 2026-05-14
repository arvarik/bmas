"use client";

import { useState, useCallback } from "react";
import { useBlackboard, selectTasks } from "@/hooks/useBlackboard";
import { Panel } from "@/components/ui/Panel";
import { ActionButton } from "@/components/ui/ActionButton";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useToast } from "@/hooks/useToast";
import { Send, Hand, Lightbulb } from "lucide-react";

// ── Types ─────────────────────────────────────────────────────────────

interface HitlState { paused: boolean; }

// ── Task Submission Section ───────────────────────────────────────────

function TaskSubmissionSection() {
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const { toast } = useToast();

  const handleSubmit = useCallback(async () => {
    if (!task.trim() || submitting) return;
    setSubmitting(true);
    try {
      const res = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: task.trim() }),
      });
      if (res.ok) {
        const data = (await res.json()) as { task_id?: string };
        toast({ type: "success", message: `Task ${data.task_id ?? ""} submitted.` });
        setTask("");
      } else {
        const body = (await res.json().catch(() => ({}))) as { error?: string };
        toast({ type: "error", message: body.error ?? `HTTP ${res.status}` });
      }
    } catch (err) {
      toast({ type: "error", message: err instanceof Error ? err.message : "Network error" });
    } finally {
      setSubmitting(false);
    }
  }, [task, submitting, toast]);

  return (
    <Panel title="Submit Task" subtitle="Send a new task to the swarm">
      <div className="operator-section">
        <textarea
          id="operator-task-input"
          value={task}
          onChange={(e) => setTask(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && (e.metaKey || e.ctrlKey)) {
              e.preventDefault();
              void handleSubmit();
            }
          }}
          placeholder="Describe a task for the swarm to execute…"
          disabled={submitting}
          rows={4}
          className="operator-textarea"
        />
        <div className="operator-submit-row">
          <span className="operator-hint-text">⌘ + Enter to submit</span>
          <ActionButton
            variant="primary"
            loading={submitting}
            disabled={!task.trim()}
            onClick={handleSubmit}
          >
            <Send size={14} /> Submit Task
          </ActionButton>
        </div>
      </div>
    </Panel>
  );
}

// ── Swarm Control Section ─────────────────────────────────────────────

function SwarmControlSection() {
  const [paused, setPaused] = useState(false);
  const [toggling, setToggling] = useState(false);
  const [daemonError, setDaemonError] = useState<string | null>(null);
  const { toast } = useToast();
  const daemonState = useBlackboard((s) => s.state);

  // Poll pause state
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

  // Start polling on mount
  useState(() => {
    const timer = setTimeout(() => void fetchState(), 0);
    const id = setInterval(fetchState, 3_000);
    return () => { clearTimeout(timer); clearInterval(id); };
  });

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

  return (
    <Panel title="Swarm Control" subtitle="Pause, resume, and monitor the swarm">
      <div className="operator-section">
        {/* Error banner */}
        {daemonError && (
          <div className="operator-error-banner">
            ⚠ {daemonError}
          </div>
        )}

        <div className="operator-control-row">
          <div className="operator-status-info">
            <span className="operator-label">Status</span>
            <StatusBadge status={paused ? "paused" : "running"} label={paused ? "Paused" : "Running"} />
          </div>

          {daemonState && (
            <div className="operator-status-info">
              <span className="operator-label">Phase</span>
              <span className="operator-value">{daemonState.phase ?? "—"}</span>
            </div>
          )}

          {daemonState && (
            <div className="operator-status-info">
              <span className="operator-label">Iteration</span>
              <span className="operator-value">{daemonState.iteration ?? 0}</span>
            </div>
          )}
        </div>

        <ActionButton
          variant={paused ? "primary" : "danger"}
          loading={toggling}
          onClick={togglePause}
          style={{ width: "100%" }}
        >
          {paused ? "▶ Resume Swarm" : "⏸ Pause Swarm"}
        </ActionButton>
      </div>
    </Panel>
  );
}

// ── Hint Injection Section ────────────────────────────────────────────

function HintInjectionSection() {
  const tasks = useBlackboard(selectTasks);
  const [selectedTask, setSelectedTask] = useState("");
  const [hintText, setHintText] = useState("");
  const [sending, setSending] = useState(false);
  const { toast } = useToast();

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
    <Panel title="Inject Hint" subtitle="Send guidance to a specific task">
      <div className="operator-section">
        <select
          id="operator-task-selector"
          value={selectedTask}
          onChange={(e) => setSelectedTask(e.target.value)}
          className="operator-select"
        >
          <option value="">Select task…</option>
          {tasks.map((t) => (
            <option key={t.id} value={t.id}>{t.label} ({t.status})</option>
          ))}
        </select>

        <textarea
          id="operator-hint-input"
          value={hintText}
          onChange={(e) => setHintText(e.target.value)}
          placeholder="Enter guidance for the swarm…"
          rows={3}
          className="operator-textarea"
        />

        <ActionButton
          variant="secondary"
          loading={sending}
          disabled={!selectedTask || !hintText.trim()}
          onClick={injectHint}
          style={{ width: "100%" }}
        >
          <Lightbulb size={14} /> Send Hint
        </ActionButton>
      </div>
    </Panel>
  );
}

// ── Main View ─────────────────────────────────────────────────────────

export default function OperatorView() {
  return (
    <div className="view-container operator-view">
      <TaskSubmissionSection />
      <SwarmControlSection />
      <HintInjectionSection />
    </div>
  );
}
