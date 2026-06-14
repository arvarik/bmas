"use client";

/**
 * ArtifactBrowser — file tree view of agent-created outputs.
 *
 * Shows a table of artifacts for a task with: filename, author, turn,
 * version badge, and download button. Fetches from /api/tasks/{taskId}/artifacts.
 */

import React, { useEffect, useState } from "react";
import {
  File, FileText, Image, Code, Download, FolderTree,
} from "lucide-react";

interface Artifact {
  id: string;
  rel_path: string;
  mime: string | null;
  bytes: number;
  sha256: string;
  version: number;
  author: string | null;
  turn_id: string | null;
  created_at: string;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function ArtifactIcon({ mime, path }: { mime: string | null; path: string }) {
  const ext = path.split(".").pop()?.toLowerCase() || "";
  if (["py", "js", "ts", "tsx", "rs", "go", "java", "c", "cpp", "rb"].includes(ext))
    return <Code size={14} />;
  // eslint-disable-next-line jsx-a11y/alt-text -- Lucide SVG icon, not an HTML img
  if (mime?.startsWith("image/")) return <Image size={14} aria-hidden="true" />;
  if (mime === "application/pdf" || mime?.startsWith("text/"))
    return <FileText size={14} />;
  return <File size={14} />;
}

export function ArtifactBrowser({ taskId }: { taskId: string }) {
  const [artifacts, setArtifacts] = useState<Artifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/tasks/${taskId}/artifacts`, {
          cache: "no-store",
        });
        if (res.ok) {
          const data = await res.json();
          if (!cancelled) setArtifacts(data.artifacts || []);
        } else if (res.status === 404) {
          // 404 means task has no artifacts — show empty state, not error
          if (!cancelled) setArtifacts([]);
        } else {
          if (!cancelled) setError(`Failed to load artifacts (${res.status})`);
        }
      } catch {
        if (!cancelled) setError("Failed to load artifacts");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [taskId]);

  if (loading) {
    return (
      <div className="artifact-browser artifact-browser--loading">
        <div className="artifact-browser__spinner" />
        <span>Loading artifacts…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="artifact-browser artifact-browser--error">
        <span>{error}</span>
      </div>
    );
  }

  if (artifacts.length === 0) {
    return (
      <div className="artifact-browser artifact-browser--empty">
        <FolderTree size={32} strokeWidth={1.5} />
        <p>No artifacts yet.</p>
        <span>Agent-created files will appear here as they are produced.</span>
      </div>
    );
  }

  // Group by directory
  const grouped = new Map<string, Artifact[]>();
  for (const a of artifacts) {
    const dir = a.rel_path.includes("/")
      ? a.rel_path.substring(0, a.rel_path.lastIndexOf("/"))
      : ".";
    if (!grouped.has(dir)) grouped.set(dir, []);
    grouped.get(dir)!.push(a);
  }

  return (
    <div className="artifact-browser">
      <div className="artifact-browser__header">
        <h3>
          <FolderTree size={16} /> Artifacts
          <span className="artifact-browser__count">{artifacts.length}</span>
        </h3>
      </div>

      <div className="artifact-browser__tree">
        {Array.from(grouped.entries()).map(([dir, files]) => (
          <div key={dir} className="artifact-browser__group">
            {dir !== "." && (
              <div className="artifact-browser__dir-label">{dir}/</div>
            )}
            {files.map((a) => {
              const filename = a.rel_path.includes("/")
                ? a.rel_path.substring(a.rel_path.lastIndexOf("/") + 1)
                : a.rel_path;

              return (
                <div key={a.id} className="artifact-browser__row">
                  <ArtifactIcon mime={a.mime} path={a.rel_path} />
                  <span className="artifact-browser__name" title={a.rel_path}>
                    {filename}
                  </span>
                  {a.version > 1 && (
                    <span className="artifact-browser__version">
                      v{a.version}
                    </span>
                  )}
                  {a.author && (
                    <span className="artifact-browser__author">{a.author}</span>
                  )}
                  <span className="artifact-browser__size">
                    {formatBytes(a.bytes)}
                  </span>
                  <a
                    href={`/api/tasks/${taskId}/artifacts/${a.id}`}
                    download={filename}
                    className="artifact-browser__download"
                    title="Download"
                  >
                    <Download size={14} />
                  </a>
                </div>
              );
            })}
          </div>
        ))}
      </div>
    </div>
  );
}
