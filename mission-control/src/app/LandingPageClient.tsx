"use client";

/**
 * LandingPageClient — the full landing page UI.
 *
 * Centered, conversational task submission interface with:
 * - Hero heading with project name
 * - Auto-resizing textarea with inline send button
 * - Example task pills
 * - Recent tasks list (clickable → /task/{id})
 * - Footer stats (agents online, total cost, task count)
 * - Optimistic submit flow via PendingTaskContext
 *
 */

import React, { useState, useCallback, useRef } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { useTaskHistory } from "@/hooks/useTaskHistory";
import { useSystemStream } from "@/hooks/useSystemStream";
import { usePendingTask } from "@/contexts/PendingTaskContext";
import { useToast } from "@/hooks/useToast";
import { ArrowUp, Zap, DollarSign, BarChart3 } from "lucide-react";

// ── Example tasks ─────────────────────────────────────────────────────

const EXAMPLE_TASKS = [
  {
    label: "Analyze competitor pricing",
    prompt:
      "Analyze the pricing strategies of our top 3 competitors and recommend positioning adjustments.",
  },
  {
    label: "Write project documentation",
    prompt:
      "Write comprehensive API documentation for our /users and /tasks REST endpoints, including request/response schemas and examples.",
  },
  {
    label: "Debug this error log",
    prompt:
      'Analyze this error trace and identify the root cause:\n\nTypeError: Cannot read property \'id\' of undefined\n  at processTask (orchestrator.js:142)\n  at async handler (api.js:28)',
  },
  {
    label: "Research market trends",
    prompt:
      "Research and summarize the key market trends for Q3 2026 in the AI infrastructure space, with focus on self-hosted solutions.",
  },
];

// ── Status indicator ──────────────────────────────────────────────────

function TaskStatusDot({ status }: { status: string }) {
  switch (status) {
    case "running":
      return (
        <span
          className="landing__status-dot pulse-dot"
          style={{ background: "var(--status-running)" }}
        />
      );
    case "completed":
      return (
        <span className="landing__status-icon" style={{ color: "var(--status-success)" }}>
          ✓
        </span>
      );
    case "failed":
      return (
        <span className="landing__status-icon" style={{ color: "var(--status-error)" }}>
          ✗
        </span>
      );
    default:
      return (
        <span className="landing__status-icon" style={{ color: "var(--status-pending)" }}>
          ○
        </span>
      );
  }
}

// ── Relative time ─────────────────────────────────────────────────────

function formatRelativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)}h ago`;
  return new Date(iso).toLocaleDateString();
}

// ── Landing Page ──────────────────────────────────────────────────────

export function LandingPageClient({ projectName }: { projectName: string }) {
  const [task, setTask] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const router = useRouter();
  const { toast } = useToast();
  const { setPending } = usePendingTask();
  const { tasks, total, isLoading } = useTaskHistory();
  const { agentHealth } = useSystemStream();

  // Count agents online
  const agentsOnline = Object.values(agentHealth).filter((a) => a.alive).length;
  const totalCost = tasks.reduce((sum, t) => sum + (t.total_cost_usd ?? 0), 0);
  const recentTasks = tasks.slice(0, 5);

  // ── Auto-resize textarea ──────────────────────────────────────────
  const maxHeight =
    typeof window !== "undefined" ? window.innerHeight * 0.4 : 300;

  const handleInput = useCallback(
    (e: React.ChangeEvent<HTMLTextAreaElement>) => {
      const el = e.target;
      el.style.height = "auto";
      el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
      setTask(el.value);
    },
    [maxHeight]
  );

  // (Removed auto-focus on mount so it doesn't highlight on refresh)

  // ── Submit handler ────────────────────────────────────────────────
  const handleSubmit = useCallback(async () => {
    const input = task.trim();
    if (!input || submitting) return;
    setSubmitting(true);

    try {
      const res = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ task: input }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(
          (body as { error?: string }).error ?? `HTTP ${res.status}`
        );
      }

      const data = (await res.json()) as { task_id?: string };
      if (data.task_id) {
        // Push optimistic state BEFORE navigating
        setPending({
          taskId: data.task_id,
          inputText: input,
          submittedAt: Date.now(),
        });
        setTask("");
        router.push(`/task/${data.task_id}`);
      }
    } catch (err) {
      toast({
        type: "error",
        message: err instanceof Error ? err.message : "Submission failed",
      });
    } finally {
      setSubmitting(false);
    }
  }, [task, submitting, setPending, router, toast]);

  // ── Example pill click ────────────────────────────────────────────
  const handleExampleClick = useCallback((prompt: string) => {
    setTask(prompt);
    const el = textareaRef.current;
    if (el) {
      el.focus();
      // Trigger auto-resize for the populated text
      setTimeout(() => {
        el.style.height = "auto";
        el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
      }, 0);
    }
  }, [maxHeight]);

  const hasInput = task.trim().length > 0;

  return (
    <div className="landing">
      <div className="landing__container">
        {/* ── Hero ──────────────────────────────────────────────── */}
        <div className="landing__hero">
          <h1 className="landing__title">{projectName}</h1>
          <p className="landing__subtitle">
            What should the swarm work on?
          </p>
        </div>

        {/* ── Input Card ────────────────────────────────────────── */}
        <div className="landing__input-card">
          <textarea
            ref={textareaRef}
            className="landing__textarea"
            value={task}
            onChange={handleInput}
            onKeyDown={(e) => {
              if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                e.preventDefault();
                void handleSubmit();
              }
            }}
            placeholder="Describe a task for the swarm to execute…"
            rows={3}
            disabled={submitting}
          />
          <button
            className={`landing__send-btn ${hasInput ? "landing__send-btn--active" : ""}`}
            onClick={handleSubmit}
            disabled={!hasInput || submitting}
            aria-label="Submit task"
            title="Submit task (⌘+Enter)"
          >
            {submitting ? (
              <span
                className="spin"
                style={{
                  width: 14,
                  height: 14,
                  border: "2px solid currentColor",
                  borderTopColor: "transparent",
                  borderRadius: "var(--radius-full)",
                  display: "inline-block",
                }}
              />
            ) : (
              <ArrowUp size={16} />
            )}
          </button>
        </div>

        {/* ── Shortcut Hint ─────────────────────────────────────── */}
        <div className="landing__shortcut-hint">⌘ + Enter to submit</div>

        {/* ── Example Pills ─────────────────────────────────────── */}
        <div className="landing__example-pills">
          {EXAMPLE_TASKS.map((ex) => (
            <button
              key={ex.label}
              className="landing__example-pill"
              onClick={() => handleExampleClick(ex.prompt)}
            >
              {ex.label}
            </button>
          ))}
        </div>

        {/* ── Recent Tasks ──────────────────────────────────────── */}
        {recentTasks.length > 0 && (
          <div className="landing__recent">
            <h3 className="landing__recent-title">Recent Tasks</h3>
            <div className="landing__recent-list">
              {recentTasks.map((t) => (
                <Link
                  key={t.id}
                  href={`/task/${t.id}`}
                  className="landing__recent-item"
                >
                  <TaskStatusDot status={t.status} />
                  <span className="landing__recent-id">{t.id}</span>
                  <span className="landing__recent-label">{t.label}</span>
                  <span className="landing__recent-time">
                    {formatRelativeTime(t.created_at)}
                  </span>
                  <span className="landing__recent-cost">
                    ${(t.total_cost_usd ?? 0).toFixed(3)}
                  </span>
                </Link>
              ))}
            </div>
          </div>
        )}

        {/* ── Empty state ───────────────────────────────────────── */}
        {!isLoading && recentTasks.length === 0 && (
          <div className="landing__empty">
            No tasks yet. Submit your first task to see the swarm in action.
          </div>
        )}

        {/* ── Footer Stats ──────────────────────────────────────── */}
        <div className="landing__stats">
          <span className="landing__stat">
            <Zap size={14} />
            {agentsOnline} agent{agentsOnline !== 1 ? "s" : ""} online
          </span>
          <span className="landing__stat">
            <DollarSign size={14} />
            ${totalCost.toFixed(2)} total
          </span>
          <span className="landing__stat">
            <BarChart3 size={14} />
            {total} task{total !== 1 ? "s" : ""}
          </span>
        </div>
      </div>
    </div>
  );
}
