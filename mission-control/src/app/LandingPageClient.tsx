"use client";

/**
 * LandingPageClient — the task composer.
 *
 * One centered composer: a growing text area, an attach button, a runtime
 * picker, and a send button. Enter sends; Shift+Enter adds a line.
 * Readiness comes from the shared ReadinessContext (top-bar system button).
 */

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import Image from "next/image";
import { ArrowUp, Paperclip, X } from "lucide-react";
import { usePendingTask } from "@/contexts/PendingTaskContext";
import { useReadiness } from "@/contexts/ReadinessContext";
import { usePreferences } from "@/lib/preferences";
import { useToast } from "@/hooks/useToast";
import { VariantSelect } from "@/components/features/VariantSelect";
import { EffortSelect } from "@/components/features/EffortSelect";
import {
  addAttachments,
  createTaskSubmissionRequest,
  MAX_TASK_ATTACHMENTS,
  submissionErrorMessage,
} from "@/lib/task-submission";

const MAX_TEXTAREA_RATIO = 0.45;

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function LandingPageClient({
  storageEnabled,
  maxUploadMb,
  allowedUploadTypes,
}: {
  storageEnabled: boolean;
  maxUploadMb: number;
  allowedUploadTypes: string[];
}) {
  const [preferences, setPreferences] = usePreferences();
  const [task, setTask] = useState("");
  const [variant, setVariant] = useState(preferences.defaultRuntime);
  const [effort, setEffort] = useState(preferences.defaultEffort);
  const [confirmEffort, setConfirmEffort] = useState("");
  const [variantAvailable, setVariantAvailable] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [attachedFiles, setAttachedFiles] = useState<File[]>([]);
  const [attachmentNotice, setAttachmentNotice] = useState("");
  const [dragActive, setDragActive] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const router = useRouter();
  const { toast } = useToast();
  const { setPending } = usePendingTask();
  const readiness = useReadiness();
  const stackReady = readiness.ready;

  const resizeTextarea = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    const maxHeight = window.innerHeight * MAX_TEXTAREA_RATIO;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, maxHeight)}px`;
  }, []);

  useEffect(() => {
    resizeTextarea();
  }, [resizeTextarea, task]);

  const handleSubmit = useCallback(async () => {
    const input = task.trim();
    if (!input || submitting || !variantAvailable || !stackReady) return;
    // A long-horizon run costs real money and time: ask once.
    if (effort === "exhaustive" && confirmEffort !== "exhaustive") {
      setConfirmEffort("exhaustive");
      return;
    }
    setConfirmEffort("");
    setSubmitting(true);
    try {
      const res = await fetch(
        "/api/submit",
        createTaskSubmissionRequest(input, variant, attachedFiles, effort),
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(submissionErrorMessage(body, res.status));
      }
      const data = (await res.json()) as { task_id?: string };
      if (data.task_id) {
        setPending({ taskId: data.task_id, inputText: input, submittedAt: Date.now() });
        setTask("");
        setAttachedFiles([]);
        setAttachmentNotice("");
        router.push(`/task/${data.task_id}`);
      }
    } catch (err) {
      toast({ type: "error", message: err instanceof Error ? err.message : "Submission failed" });
    } finally {
      setSubmitting(false);
    }
  }, [attachedFiles, confirmEffort, effort, router, setPending, stackReady, submitting, task, toast, variant, variantAvailable]);

  const acceptAttachments = useCallback((candidates: readonly File[]) => {
    if (candidates.length === 0) return;
    if (!storageEnabled) {
      toast({ type: "error", message: "Attachments are unavailable because storage is disabled." });
      return;
    }
    const selection = addAttachments(attachedFiles, candidates, allowedUploadTypes, maxUploadMb);
    setAttachedFiles(selection.files);
    if (selection.errors.length > 0) {
      const message = selection.errors.join(" ");
      setAttachmentNotice(message);
      toast({ type: "error", message });
    } else {
      setAttachmentNotice("");
    }
  }, [allowedUploadTypes, attachedFiles, maxUploadMb, storageEnabled, toast]);

  const hasInput = task.trim().length > 0;
  const canSubmit = hasInput && variantAvailable && stackReady && !submitting;
  const blocker = submitting
    ? "Submitting…"
    : readiness.loading && !readiness.document
      ? "Checking services…"
      : !stackReady
        ? "Setup needs attention. Open the system status in the top bar."
        : !variantAvailable
          ? "Select an available runtime."
          : "";

  const attachTitle = storageEnabled
    ? `Attach up to ${MAX_TASK_ATTACHMENTS} files, ${maxUploadMb} MB each (${allowedUploadTypes.join(", ")})`
    : "Enable storage in bmas.yaml to attach files";

  return (
    <div className="composer-page">
      <div className="composer">
        <div className="composer__hero">
          <Image
            src="/ant-head.png"
            alt=""
            className="composer__logo animate-float"
            width={72}
            height={72}
            loading="eager"
            priority
          />
          <h1 className="composer__title">Stigmergic</h1>
        </div>

        <div
          className={`composer__card ${dragActive ? "composer__card--drag" : ""} ${submitting ? "composer__card--busy" : ""}`}
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
            if (!event.currentTarget.contains(event.relatedTarget as Node | null)) setDragActive(false);
          }}
          onDrop={(event) => {
            if (!event.dataTransfer.types.includes("Files")) return;
            event.preventDefault();
            setDragActive(false);
            acceptAttachments(Array.from(event.dataTransfer.files));
          }}
          onClick={(event) => {
            if (event.target === event.currentTarget) textareaRef.current?.focus();
          }}
        >
          {dragActive ? <div className="composer__drop" aria-hidden="true">Drop files to attach</div> : null}

          {attachedFiles.length > 0 ? (
            <ul className="composer__files" aria-label={`${attachedFiles.length} attached files`}>
              {attachedFiles.map((file, index) => (
                <li key={`${file.name}:${file.size}:${file.lastModified}`} className="composer__file">
                  <Paperclip size={12} aria-hidden="true" />
                  <span className="composer__file-name">{file.name}</span>
                  <span className="composer__file-size">{formatBytes(file.size)}</span>
                  <button
                    type="button"
                    className="composer__file-remove"
                    aria-label={`Remove ${file.name}`}
                    onClick={() => setAttachedFiles((previous) => previous.filter((_, i) => i !== index))}
                  >
                    <X size={13} aria-hidden="true" />
                  </button>
                </li>
              ))}
            </ul>
          ) : null}

          <label htmlFor="task-objective" className="sr-only">Task</label>
          <textarea
            id="task-objective"
            ref={textareaRef}
            className="composer__input"
            value={task}
            onChange={(event) => setTask(event.target.value)}
            onKeyDown={(event) => {
              if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
              const modifier = event.metaKey || event.ctrlKey;
              const sends = preferences.sendKey === "mod-enter" ? modifier : (!event.shiftKey && !event.altKey) || modifier;
              if (!sends) return;
              event.preventDefault();
              void handleSubmit();
            }}
            onPaste={(event) => {
              const files = Array.from(event.clipboardData.items)
                .filter((item) => item.kind === "file")
                .map((item) => item.getAsFile())
                .filter((file): file is File => file !== null);
              if (files.length === 0) return;
              event.preventDefault();
              acceptAttachments(files);
            }}
            placeholder="What should the agents work on?"
            rows={1}
            disabled={submitting}
            aria-describedby={attachmentNotice || blocker ? "composer-notice" : undefined}
          />

          <input
            ref={fileInputRef}
            type="file"
            multiple
            accept={allowedUploadTypes.map((type) => `.${type}`).join(",")}
            className="sr-only"
            tabIndex={-1}
            onChange={(event) => {
              acceptAttachments(Array.from(event.target.files || []));
              event.target.value = "";
            }}
          />

          <div className="composer__toolbar">
            <div className="composer__tools">
              <button
                type="button"
                className="composer__tool"
                onClick={() => fileInputRef.current?.click()}
                disabled={submitting || !storageEnabled}
                title={attachTitle}
                aria-label={`Attach files. ${attachTitle}`}
              >
                <Paperclip size={17} aria-hidden="true" />
              </button>
              <VariantSelect
                value={variant}
                onChange={setVariant}
                onAvailabilityChange={setVariantAvailable}
              />
              <EffortSelect
                variant={variant}
                value={effort}
                onChange={(next) => {
                  setEffort(next);
                  setConfirmEffort("");
                  setPreferences({ defaultEffort: next });
                }}
              />
            </div>
            <button
              type="button"
              className={`composer__send ${canSubmit ? "composer__send--ready" : ""}`}
              onClick={() => void handleSubmit()}
              disabled={!canSubmit}
              aria-label={submitting ? "Submitting task" : "Send task"}
              title={blocker || (preferences.sendKey === "mod-enter" ? "Send (⌘ Enter)" : "Send (Enter)")}
            >
              {submitting ? <span className="composer__spinner" aria-hidden="true" /> : <ArrowUp size={18} aria-hidden="true" />}
            </button>
          </div>
        </div>

        {confirmEffort ? (
          <div className="composer__confirm" role="alertdialog" aria-label="Confirm exhaustive run">
            <span>
              Exhaustive runs many rounds under a high budget ceiling and can
              take an hour. Press send again to start, or pick a lower effort.
            </span>
            <button type="button" className="button" onClick={() => setConfirmEffort("")}>Cancel</button>
          </div>
        ) : null}
        <p id="composer-notice" className={`composer__notice ${attachmentNotice ? "composer__notice--error" : ""}`} aria-live="polite">
          {attachmentNotice || (hasInput ? blocker : "")}
        </p>
      </div>
    </div>
  );
}
