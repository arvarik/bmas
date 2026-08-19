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
import Image from "next/image";
import { useTaskHistory } from "@/hooks/useTaskHistory";
import { useSystemStream } from "@/hooks/useSystemStream";
import { usePendingTask } from "@/contexts/PendingTaskContext";
import { useToast } from "@/hooks/useToast";
import { ArrowUp, Zap, DollarSign, BarChart3, Paperclip, Cpu, Cloud, X } from "lucide-react";
import { VariantSelect } from "@/components/features/VariantSelect";
import { ReadinessPanel } from "@/components/features/ReadinessPanel";
import {
  addAttachments,
  createTaskSubmissionRequest,
  MAX_TASK_ATTACHMENTS,
  submissionErrorMessage,
} from "@/lib/task-submission";

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
  const label = status === "pending" ? "Queued" : status.charAt(0).toUpperCase() + status.slice(1);
  switch (status) {
    case "running":
      return (
        <span className="landing__recent-status">
          <span
            className="landing__status-dot pulse-dot"
            style={{ background: "var(--status-running)" }}
            aria-hidden="true"
          />
          {label}
        </span>
      );
    case "completed":
      return (
        <span className="landing__recent-status">
          <span className="landing__status-icon" style={{ color: "var(--status-success)" }} aria-hidden="true">✓</span>
          {label}
        </span>
      );
    case "failed":
      return (
        <span className="landing__recent-status">
          <span className="landing__status-icon" style={{ color: "var(--status-error)" }} aria-hidden="true">✗</span>
          {label}
        </span>
      );
    default:
      return (
        <span className="landing__recent-status">
          <span className="landing__status-icon" style={{ color: "var(--status-pending)" }} aria-hidden="true">○</span>
          {label}
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

export function LandingPageClient({
  projectName,
  storageEnabled,
  maxUploadMb,
  allowedUploadTypes,
}: {
  projectName: string;
  storageEnabled: boolean;
  maxUploadMb: number;
  allowedUploadTypes: string[];
}) {
  const [task, setTask] = useState("");
  const [variant, setVariant] = useState("classic");
  const [variantAvailable, setVariantAvailable] = useState(false);
  const [stackReady, setStackReady] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [attachmentFeedback, setAttachmentFeedback] = useState("");
  const [attachmentError, setAttachmentError] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

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
    if (!input || submitting || !variantAvailable || !stackReady) return;
    setSubmitting(true);

    try {
      const res = await fetch(
        "/api/submit",
        createTaskSubmissionRequest(input, variant, attachedFiles),
      );

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(submissionErrorMessage(body, res.status));
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
        setAttachedFiles([]);
        setAttachmentFeedback("");
        setAttachmentError(false);
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
  }, [task, variant, variantAvailable, stackReady, submitting, attachedFiles, setPending, router, toast]);

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
  const canSubmit = hasInput && variantAvailable && stackReady && !submitting;

  const acceptAttachments = useCallback((candidates: readonly File[], source: string) => {
    if (candidates.length === 0) return;
    if (!storageEnabled) {
      setAttachmentFeedback("Attachments are unavailable because storage is disabled.");
      setAttachmentError(true);
      return;
    }
    const selection = addAttachments(
      attachedFiles,
      candidates,
      allowedUploadTypes,
      maxUploadMb,
    );
    setAttachedFiles(selection.files);
    const addedCount = selection.files.length - attachedFiles.length;
    if (selection.errors.length > 0) {
      const addedMessage = addedCount > 0
        ? `${addedCount} ${addedCount === 1 ? "file was" : "files were"} added. `
        : "";
      const message = `${addedMessage}${selection.errors.join(" ")}`;
      setAttachmentFeedback(message);
      setAttachmentError(true);
      toast({ type: "error", message });
      return;
    }
    setAttachmentFeedback(
      `${addedCount} ${addedCount === 1 ? "file" : "files"} added from ${source}.`,
    );
    setAttachmentError(false);
  }, [allowedUploadTypes, attachedFiles, maxUploadMb, storageEnabled, toast]);

  const attachmentHelp = storageEnabled
    ? `Attach up to ${MAX_TASK_ATTACHMENTS} files. Each file can be up to ${maxUploadMb} MB. Allowed types: ${allowedUploadTypes.join(", ")}. You can choose, drop, or paste files.`
    : "Attachments are unavailable because storage is disabled.";

  return (
    <div className="landing">
      <div className="landing__container">
        {/* ── Hero ──────────────────────────────────────────────── */}
        <div className="landing__hero">
          <div className="landing__logo-container">
            <Image
              src="/ant-head.png"
              alt="bMAS Swarm Logo"
              className="landing__logo animate-float"
              width={96}
              height={96}
              loading="eager"
            />
          </div>
          <h1 className="landing__title">{projectName}</h1>
          <p className="landing__subtitle">
            What should the swarm work on?
          </p>
        </div>

        <ReadinessPanel
          onReadyChange={setStackReady}
          showReadyGuide={!isLoading && total === 0}
        />

        {/* ── Input Card ────────────────────────────────────────── */}
        <div
          className={`landing__input-card ${dragActive ? "landing__input-card--drag-active" : ""}`}
          onDragEnter={(event) => {
            if (event.dataTransfer.types.includes("Files")) {
              event.preventDefault();
              setDragActive(true);
            }
          }}
          onDragOver={(event) => {
            if (event.dataTransfer.types.includes("Files")) event.preventDefault();
          }}
          onDragLeave={(event) => {
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
              setDragActive(false);
            }
          }}
          onDrop={(event) => {
            if (!event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            setDragActive(false);
            acceptAttachments(Array.from(event.dataTransfer.files), "drop");
          }}
        >
          {dragActive ? (
            <div className="landing__drop-message" aria-hidden="true">Drop files to attach them</div>
          ) : null}
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
            onPaste={(event) => {
              const files = Array.from(event.clipboardData.items)
                .filter((item) => item.kind === "file")
                .map((item) => item.getAsFile())
                .filter((file): file is File => file !== null);
              if (files.length === 0) return;
              event.preventDefault();
              acceptAttachments(files, "the clipboard");
            }}
            placeholder="Describe a task for the swarm to execute…"
            rows={3}
            disabled={submitting}
            aria-describedby="attachment-help attachment-feedback"
          />
          {/* Hidden file input */}
          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={allowedUploadTypes.map((type) => `.${type}`).join(",")}
            style={{ display: "none" }}
            onChange={(e) => {
              const files = Array.from(e.target.files || []);
              acceptAttachments(files, "the file picker");
              e.target.value = "";
            }}
          />
          {/* Toolbar row: attach + variant on left, send on right */}
          <div className="landing__toolbar">
            <div className="landing__toolbar-left">
              <button
                className="landing__attach-btn"
                onClick={() => fileInputRef.current?.click()}
                disabled={submitting || !storageEnabled}
                title={
                  storageEnabled
                    ? `Attach files, up to ${maxUploadMb} MB each`
                    : "Enable storage in bmas.yaml to attach files"
                }
                type="button"
                aria-label={`Attach files. ${attachmentHelp}`}
                aria-describedby="attachment-help"
              >
                <Paperclip size={13} />
                <span>Attach</span>
              </button>
              <VariantSelect
                value={variant}
                onChange={setVariant}
                onAvailabilityChange={setVariantAvailable}
              />
            </div>
            <div className="landing__toolbar-right">
              <span className="landing__shortcut-hint">⌘ Enter</span>
              <button
                className={`landing__send-btn ${canSubmit ? "landing__send-btn--active" : ""}`}
                onClick={handleSubmit}
                disabled={!canSubmit}
                aria-label="Submit task"
                title={stackReady ? "Submit task (⌘+Enter)" : "Complete the readiness checks first"}
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
          </div>
          <p id="attachment-help" className="landing__attachment-help">
            {attachmentHelp}
          </p>
          <p
            id="attachment-feedback"
            className="landing__attachment-feedback"
            role={attachmentError ? "alert" : "status"}
            aria-live={attachmentError ? "assertive" : "polite"}
          >
            {attachmentFeedback}
          </p>
          {/* Attached files chips */}
          {attachedFiles.length > 0 && (
            <div className="landing__attached-files" aria-label={`${attachedFiles.length} attached files`}>
              <span className="landing__attached-count">
                {attachedFiles.length} of {MAX_TASK_ATTACHMENTS} attached
              </span>
              {attachedFiles.map((f, i) => (
                <span key={`${f.name}:${f.size}:${f.lastModified}`} className="landing__attached-chip">
                  <span className="landing__attached-name">{f.name}</span>
                  <button
                    onClick={() => {
                      setAttachedFiles((previous) => previous.filter((_, index) => index !== i));
                      setAttachmentFeedback(`${f.name} removed.`);
                      setAttachmentError(false);
                    }}
                    className="landing__attached-remove"
                    type="button"
                    aria-label={`Remove ${f.name}`}
                  >
                    <X size={14} aria-hidden="true" />
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>


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
                  {t.model_used && (
                    <span
                      className="landing__recent-model"
                      title={
                        t.model_used.startsWith("edge-")
                          ? `Local inference (${t.model_used})`
                          : `Cloud API (${t.model_used})`
                      }
                    >
                      {t.model_used.startsWith("edge-")
                        ? <Cpu size={11} />
                        : <Cloud size={11} />
                      }
                    </span>
                  )}
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
