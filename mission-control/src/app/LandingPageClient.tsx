"use client";

/**
 * LandingPageClient — the full landing page UI.
 *
 * Centered, conversational task submission interface with:
 * - Hero heading with project name
 * - Auto-resizing textarea with inline send button
 * - Example task pills
 * - Operational task queues
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
import { ArrowUp, Zap, Paperclip, X } from "lucide-react";
import { VariantSelect } from "@/components/features/VariantSelect";
import { ReadinessPanel } from "@/components/features/ReadinessPanel";
import {
  addAttachments,
  buildTaskObjective,
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
      "Research the main market trends in AI infrastructure. Compare self-hosted and managed options. Cite the evidence for each conclusion.",
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
  storageEnabled,
  maxUploadMb,
  allowedUploadTypes,
}: {
  storageEnabled: boolean;
  maxUploadMb: number;
  allowedUploadTypes: string[];
}) {
  const [task, setTask] = useState("");
  const [constraints, setConstraints] = useState("");
  const [expectedOutput, setExpectedOutput] = useState("");
  const [quickMode, setQuickMode] = useState(false);
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
  const history = useTaskHistory({ sort: "activity-desc" });
  const runningHistory = useTaskHistory({ status: "running", sort: "activity-desc" });
  const attentionHistory = useTaskHistory({ status: "attention", sort: "activity-desc" });
  const { agentHealth } = useSystemStream();

  // Count agents online
  const agentsOnline = Object.values(agentHealth).filter((a) => a.alive).length;
  const failedTasks = attentionHistory.tasks.filter((item) => item.status === "failed").slice(0, 4);
  const approvalTasks = attentionHistory.tasks.filter((item) => item.pending_approval).slice(0, 4);

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
    const input = buildTaskObjective(task, constraints, expectedOutput, quickMode);
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
        setConstraints("");
        setExpectedOutput("");
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
  }, [task, constraints, expectedOutput, quickMode, variant, variantAvailable, stackReady, submitting, attachedFiles, setPending, router, toast]);

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
  const submissionReason = submitting
    ? "Submitting the task…"
    : !hasInput
      ? "Enter an objective to submit this task."
      : !variantAvailable
        ? "Select an available runtime to submit this task."
        : !stackReady
          ? "Complete the readiness checks to submit this task."
          : "Ready to submit.";

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
              alt="Stigmergic"
              className="landing__logo animate-float"
              width={96}
              height={96}
              loading="eager"
            />
          </div>
          <h1 className="landing__title">Stigmergic</h1>
          <p className="landing__subtitle">
            Mission Control for the bMAS architecture
          </p>
        </div>

        <ReadinessPanel
          onReadyChange={setStackReady}
          showReadyGuide={!history.isLoading && history.total === 0}
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
          <div className="landing__mode-row">
            <label className="landing__mode-toggle"><input type="checkbox" checked={quickMode} onChange={(event) => setQuickMode(event.target.checked)} /> Quick mode</label>
            <span>{quickMode ? "Use one field for an experienced workflow." : "Add structure so the runtime can verify the result."}</span>
          </div>
          <label className="landing__field-label" htmlFor="task-objective">Objective</label>
          <textarea
            id="task-objective"
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
            placeholder="State the result that the agents must produce…"
            rows={3}
            disabled={submitting}
            aria-describedby="attachment-help attachment-feedback"
          />
          {!quickMode ? (
            <div className="landing__guided-fields">
              <label htmlFor="task-constraints">Constraints <span>Optional</span></label>
              <textarea id="task-constraints" value={constraints} onChange={(event) => setConstraints(event.target.value)} rows={2} placeholder="List limits, required sources, exclusions, or quality rules…" disabled={submitting} />
              <label htmlFor="task-output">Expected output <span>Optional</span></label>
              <textarea id="task-output" value={expectedOutput} onChange={(event) => setExpectedOutput(event.target.value)} rows={2} placeholder="Describe the format, files, sections, or acceptance checks…" disabled={submitting} />
            </div>
          ) : null}
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
              <span className="landing__submit-reason" aria-live="polite">{submissionReason}</span>
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

        {/* ── Operational work ─────────────────────────────────── */}
        <div className="landing__operations-grid">
          <section className="landing__recent">
            <div className="landing__section-heading"><h3 className="landing__recent-title">Running tasks</h3><Link href="/activity">View live activity</Link></div>
            {runningHistory.tasks.length ? <div className="landing__recent-list">
              {runningHistory.tasks.slice(0, 4).map((t) => (
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
                </Link>
              ))}
            </div> : <p className="landing__empty">No tasks run now.</p>}
          </section>
          <section className="landing__recent">
            <div className="landing__section-heading"><h3 className="landing__recent-title">Needs attention</h3><Link href="/tasks?status=attention">View all</Link></div>
            {approvalTasks.length || failedTasks.length ? <div className="landing__recent-list">
              {[...approvalTasks, ...failedTasks.filter((taskItem) => !approvalTasks.some((approval) => approval.id === taskItem.id))].slice(0, 4).map((t) => (
                <Link key={t.id} href={`/task/${t.id}`} className="landing__recent-item">
                  <TaskStatusDot status={t.status} /><span className="landing__recent-label">{t.label}</span><span className="landing__recent-time">{t.pending_approval ? "Approval required" : t.error_message || "Failed"}</span>
                </Link>
              ))}
            </div> : <p className="landing__empty">No tasks need attention.</p>}
          </section>
        </div>

        {/* ── Footer Stats ──────────────────────────────────────── */}
        <div className="landing__stats">
          <span className="landing__stat">
            <Zap size={14} />
            {agentsOnline} agent{agentsOnline !== 1 ? "s" : ""} online
          </span>
          <Link className="landing__stat" href="/tasks">Open task history</Link>
        </div>
      </div>
    </div>
  );
}
