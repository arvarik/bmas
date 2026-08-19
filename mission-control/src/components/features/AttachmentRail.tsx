"use client";

/**
 * AttachmentRail — horizontal chip strip showing uploaded files for a task.
 *
 * Sits in the task header area. Each chip shows file icon, name, and size.
 * Click opens a slide-over with extracted text preview (for text/PDF).
 */

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { File, FileText, Image, X } from "lucide-react";
import type { TaskFile } from "@/hooks/useTaskStream";
import { useFocusTrap } from "@/hooks/useFocusTrap";

const EMPTY_TASK_FILES: readonly TaskFile[] = [];

export function mergeTaskFiles(
  saved: readonly TaskFile[],
  live: readonly TaskFile[],
): TaskFile[] {
  const merged = new Map(saved.map((file) => [file.id, file]));
  for (const file of live) {
    const previous = merged.get(file.id);
    merged.set(file.id, previous ? {
      ...previous,
      ...file,
      name: file.name || previous.name,
      mime: file.mime || previous.mime,
      sha256: file.sha256 || previous.sha256,
      created_at: file.created_at || previous.created_at,
    } : file);
  }
  return [...merged.values()];
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function FileIcon({ mime }: { mime: string }) {
  // eslint-disable-next-line jsx-a11y/alt-text -- Lucide SVG icon, not an HTML img
  if (mime.startsWith("image/")) return <Image size={14} aria-hidden="true" />;
  if (mime === "application/pdf" || mime.startsWith("text/"))
    return <FileText size={14} />;
  return <File size={14} />;
}

export function AttachmentRail({
  taskId,
  liveFiles = EMPTY_TASK_FILES,
}: {
  taskId: string;
  liveFiles?: readonly TaskFile[];
}) {
  return <TaskAttachmentRail key={taskId} taskId={taskId} liveFiles={liveFiles} />;
}

function TaskAttachmentRail({
  taskId,
  liveFiles,
}: {
  taskId: string;
  liveFiles: readonly TaskFile[];
}) {
  const [files, setFiles] = useState<TaskFile[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loadVersion, setLoadVersion] = useState(0);
  const [previewFile, setPreviewFile] = useState<TaskFile | null>(null);
  const [previewText, setPreviewText] = useState<string>("");
  const previewRequest = useRef(0);
  const previewDialogRef = useRef<HTMLDivElement>(null);
  const visibleFiles = useMemo(
    () => mergeTaskFiles(files, liveFiles),
    [files, liveFiles],
  );

  // Fetch files on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `/api/tasks/${encodeURIComponent(taskId)}/files`,
          {
            cache: "no-store",
          },
        );
        if (!res.ok) {
          throw new Error(`Attachments returned HTTP ${res.status}`);
        }
        const data = await res.json();
        if (!cancelled) {
          setFiles(data.files || []);
          setLoadError(null);
        }
      } catch (error) {
        if (!cancelled) {
          setLoadError(
            error instanceof Error ? error.message : "Attachments failed to load",
          );
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [taskId, loadVersion]);

  useEffect(() => () => {
    previewRequest.current += 1;
  }, []);

  // Fetch extracted text for preview
  const openPreview = useCallback(
    async (file: TaskFile) => {
      const requestId = previewRequest.current + 1;
      previewRequest.current = requestId;
      setPreviewFile(file);
      setPreviewText("Loading…");
      try {
        const response = await fetch(
          `/api/tasks/${encodeURIComponent(taskId)}/files/${encodeURIComponent(file.id)}/text`,
          { cache: "no-store" },
        );
        const body = await response.json().catch(() => ({})) as {
          error?: string;
          extracted_text?: string;
        };
        if (previewRequest.current !== requestId) return;
        if (!response.ok) {
          setPreviewText(body.error || `Preview failed (${response.status})`);
          return;
        }
        setPreviewText(
          body.extracted_text
            || "This file has no extracted text. Download it to view the original content.",
        );
      } catch {
        if (previewRequest.current === requestId) {
          setPreviewText("Failed to load preview");
        }
      }
    },
    [taskId],
  );

  const closePreview = useCallback(() => {
    previewRequest.current += 1;
    setPreviewFile(null);
  }, []);
  useFocusTrap({
    active: previewFile !== null,
    containerRef: previewDialogRef,
    onEscape: closePreview,
  });

  if (visibleFiles.length === 0) {
    if (!loadError) return null;
    return (
      <div className="attachment-rail" role="alert">
        <span className="attachment-rail__label">Attachments failed to load</span>
        <button
          className="attachment-rail__chip"
          type="button"
          onClick={() => {
            setLoadError(null);
            setLoadVersion((version) => version + 1);
          }}
        >
          Retry
        </button>
      </div>
    );
  }

  return (
    <>
      <div className="attachment-rail">
        <span className="attachment-rail__label">Attachments</span>
        {visibleFiles.map((f) => (
          <button
            type="button"
            key={f.id}
            className="attachment-rail__chip"
            onClick={() => openPreview(f)}
            title={`${f.name} — ${formatBytes(f.bytes)}`}
          >
            <FileIcon mime={f.mime} />
            <span className="attachment-rail__name">{f.name}</span>
            <span className="attachment-rail__size">
              {formatBytes(f.bytes)}
            </span>
          </button>
        ))}
      </div>

      {/* Preview slide-over */}
      {previewFile && (
        <div className="attachment-preview-overlay">
          <button
            type="button"
            className="attachment-preview-backdrop"
            onClick={closePreview}
            aria-label="Close attachment preview"
          />
          <div
            ref={previewDialogRef}
            className="attachment-preview"
            role="dialog"
            aria-modal="true"
            aria-label={`Preview ${previewFile.name}`}
            tabIndex={-1}
          >
            <div className="attachment-preview__header">
              <h3>{previewFile.name}</h3>
              <button
                type="button"
                onClick={closePreview}
                className="attachment-preview__close"
                aria-label="Close preview"
              >
                <X size={18} />
              </button>
            </div>
            <pre className="attachment-preview__text">{previewText}</pre>
            <a
              href={
                `/api/tasks/${encodeURIComponent(taskId)}`
                + `/files/${encodeURIComponent(previewFile.id)}`
              }
              download={previewFile.name}
              className="attachment-preview__download"
            >
              Download
            </a>
          </div>
        </div>
      )}
    </>
  );
}
