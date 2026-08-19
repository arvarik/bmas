"use client";

/**
 * ArtifactBrowser — file tree view of agent-created outputs.
 *
 * Shows a table of artifacts for a task with: filename, author, turn,
 * version badge, and download button. Fetches from /api/tasks/{taskId}/artifacts.
 */

import React, { useEffect, useMemo, useState } from "react";
import {
  File, FileText, Image, Code, Download, FolderTree,
} from "lucide-react";
import type { TaskArtifact } from "@/hooks/useTaskStream";

const EMPTY_TASK_ARTIFACTS: readonly TaskArtifact[] = [];

export function mergeTaskArtifacts(
  saved: readonly TaskArtifact[],
  live: readonly TaskArtifact[],
): TaskArtifact[] {
  const merged = new Map(saved.map((artifact) => [artifact.id, artifact]));
  for (const artifact of live) {
    const previous = merged.get(artifact.id);
    merged.set(artifact.id, previous ? {
      ...previous,
      ...artifact,
      rel_path: artifact.rel_path || previous.rel_path,
      mime: artifact.mime ?? previous.mime,
      sha256: artifact.sha256 || previous.sha256,
      author: artifact.author ?? previous.author,
      turn_id: artifact.turn_id ?? previous.turn_id,
      created_at: artifact.created_at || previous.created_at,
    } : artifact);
  }
  return [...merged.values()].sort((left, right) => (
    left.rel_path.localeCompare(right.rel_path)
    || right.version - left.version
  ));
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

export function ArtifactBrowser({
  taskId,
  liveArtifacts = EMPTY_TASK_ARTIFACTS,
}: {
  taskId: string;
  liveArtifacts?: readonly TaskArtifact[];
}) {
  return (
    <TaskArtifactBrowser
      key={taskId}
      taskId={taskId}
      liveArtifacts={liveArtifacts}
    />
  );
}

function TaskArtifactBrowser({
  taskId,
  liveArtifacts,
}: {
  taskId: string;
  liveArtifacts: readonly TaskArtifact[];
}) {
  const [artifacts, setArtifacts] = useState<TaskArtifact[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [loadVersion, setLoadVersion] = useState(0);
  const visibleArtifacts = useMemo(
    () => mergeTaskArtifacts(artifacts, liveArtifacts),
    [artifacts, liveArtifacts],
  );

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/tasks/${encodeURIComponent(taskId)}/artifacts`, {
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
  }, [taskId, loadVersion]);

  if (loading && visibleArtifacts.length === 0) {
    return (
      <div className="artifact-browser artifact-browser--loading">
        <div className="artifact-browser__spinner" />
        <span>Loading artifacts…</span>
      </div>
    );
  }

  if (error && visibleArtifacts.length === 0) {
    return (
      <div className="artifact-browser artifact-browser--error">
        <span>{error}</span>
        <button
          type="button"
          className="artifact-browser__retry"
          onClick={() => {
            setLoading(true);
            setError(null);
            setLoadVersion((version) => version + 1);
          }}
        >
          Retry
        </button>
      </div>
    );
  }

  if (visibleArtifacts.length === 0) {
    return (
      <div className="artifact-browser artifact-browser--empty">
        <FolderTree size={32} strokeWidth={1.5} />
        <p>No artifacts yet.</p>
        <span>Agent-created files will appear here as they are produced.</span>
      </div>
    );
  }

  // Group by directory
  const grouped = new Map<string, TaskArtifact[]>();
  for (const a of visibleArtifacts) {
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
          <span className="artifact-browser__count">{visibleArtifacts.length}</span>
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
                    href={
                      `/api/tasks/${encodeURIComponent(taskId)}`
                      + `/artifacts/${encodeURIComponent(a.id)}`
                    }
                    download={filename}
                    className="artifact-browser__download"
                    title={`Download ${a.rel_path} version ${a.version}`}
                    aria-label={`Download ${a.rel_path} version ${a.version}`}
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
