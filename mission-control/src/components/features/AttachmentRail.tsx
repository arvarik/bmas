"use client";

/**
 * AttachmentRail — horizontal chip strip showing uploaded files for a task.
 *
 * Sits in the task header area. Each chip shows file icon, name, and size.
 * Click opens a slide-over with extracted text preview (for text/PDF).
 */

import React, { useEffect, useState, useCallback } from "react";
import { File, FileText, Image, X } from "lucide-react";

interface TaskFile {
  id: string;
  name: string;
  mime: string;
  bytes: number;
  sha256: string;
  extracted_chars: number;
  created_at: string;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function FileIcon({ mime }: { mime: string }) {
  if (mime.startsWith("image/")) return <Image size={14} aria-hidden="true" />;
  if (mime === "application/pdf" || mime.startsWith("text/"))
    return <FileText size={14} />;
  return <File size={14} />;
}

export function AttachmentRail({ taskId }: { taskId: string }) {
  const [files, setFiles] = useState<TaskFile[]>([]);
  const [loading, setLoading] = useState(true);
  const [previewFile, setPreviewFile] = useState<TaskFile | null>(null);
  const [previewText, setPreviewText] = useState<string>("");

  // Fetch files on mount
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/tasks/${taskId}/files`, {
          cache: "no-store",
        });
        if (res.ok) {
          const data = await res.json();
          if (!cancelled) setFiles(data.files || []);
        }
      } catch {
        // ignore
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  // Fetch extracted text for preview
  const openPreview = useCallback(
    async (file: TaskFile) => {
      setPreviewFile(file);
      setPreviewText("Loading…");
      try {
        // We don't have a text endpoint via the proxy, so just show metadata
        setPreviewText(
          `File: ${file.name}\nType: ${file.mime}\nSize: ${formatBytes(file.bytes)}\nSHA-256: ${file.sha256}\nExtracted chars: ${file.extracted_chars}`
        );
      } catch {
        setPreviewText("Failed to load preview");
      }
    },
    [],
  );

  if (loading || files.length === 0) return null;

  return (
    <>
      <div className="attachment-rail">
        <span className="attachment-rail__label">Attachments</span>
        {files.map((f) => (
          <button
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
        <div className="attachment-preview-overlay" onClick={() => setPreviewFile(null)}>
          <div
            className="attachment-preview"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="attachment-preview__header">
              <h3>{previewFile.name}</h3>
              <button
                onClick={() => setPreviewFile(null)}
                className="attachment-preview__close"
                aria-label="Close preview"
              >
                <X size={18} />
              </button>
            </div>
            <pre className="attachment-preview__text">{previewText}</pre>
            <a
              href={`/api/tasks/${taskId}/files/${previewFile.id}`}
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
