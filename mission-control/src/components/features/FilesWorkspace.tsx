"use client";

import { useEffect, useMemo, useState } from "react";
import Image from "next/image";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  Code2,
  Columns2,
  Download,
  File,
  FileImage,
  FileJson,
  FileText,
  GitCompare,
  History,
  RefreshCw,
} from "lucide-react";
import type { TaskArtifact, TaskFile } from "@/hooks/useTaskStream";
import { mergeTaskFiles } from "@/components/features/AttachmentRail";
import { mergeTaskArtifacts } from "@/components/features/ArtifactBrowser";
import { ActionableError } from "@/components/ui/ActionableError";
import { Select } from "@/components/ui/Select";

const EMPTY_FILES: readonly TaskFile[] = [];
const EMPTY_ARTIFACTS: readonly TaskArtifact[] = [];
const MAX_TEXT_PREVIEW_BYTES = 1_000_000;

type PreviewKind = "pdf" | "image" | "json" | "markdown" | "code" | "text" | "unsupported";
type Selection =
  | { type: "input"; value: TaskFile }
  | { type: "output"; value: TaskArtifact };

const CODE_EXTENSIONS = new Set([
  "c", "cpp", "css", "go", "h", "html", "java", "js", "jsx", "kt",
  "php", "py", "rb", "rs", "sh", "sql", "swift", "toml", "ts", "tsx",
  "xml", "yaml", "yml",
]);

export function previewKind(name: string, mime: string | null): PreviewKind {
  const extension = name.split(".").pop()?.toLowerCase() ?? "";
  if (mime === "application/pdf" || extension === "pdf") return "pdf";
  if (mime?.startsWith("image/") || ["gif", "jpeg", "jpg", "png", "svg", "webp"].includes(extension)) return "image";
  if (mime === "application/json" || extension === "json") return "json";
  if (["md", "mdx", "markdown"].includes(extension)) return "markdown";
  if (CODE_EXTENSIONS.has(extension)) return "code";
  if (mime?.startsWith("text/") || ["csv", "log", "txt"].includes(extension)) return "text";
  return "unsupported";
}

export function groupArtifactVersions(
  artifacts: readonly TaskArtifact[],
): Map<string, TaskArtifact[]> {
  const groups = new Map<string, TaskArtifact[]>();
  for (const artifact of artifacts) {
    const versions = groups.get(artifact.rel_path) ?? [];
    versions.push(artifact);
    groups.set(artifact.rel_path, versions);
  }
  for (const versions of groups.values()) {
    versions.sort((left, right) => right.version - left.version);
  }
  return groups;
}

export async function readTextPreview(
  response: Response,
  maxBytes = MAX_TEXT_PREVIEW_BYTES,
): Promise<{ text: string; truncated: boolean }> {
  if (!response.body) {
    const text = await response.text();
    return { text: text.slice(0, maxBytes), truncated: text.length > maxBytes };
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let received = 0;
  let text = "";
  let truncated = false;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    const remaining = maxBytes - received;
    if (value.byteLength > remaining) {
      text += decoder.decode(value.subarray(0, Math.max(remaining, 0)), { stream: true });
      truncated = true;
      await reader.cancel();
      break;
    }
    received += value.byteLength;
    text += decoder.decode(value, { stream: true });
  }
  text += decoder.decode();
  return { text, truncated };
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function ItemIcon({ name, mime }: { name: string; mime: string | null }) {
  const kind = previewKind(name, mime);
  if (kind === "image") return <FileImage size={16} />;
  if (kind === "json") return <FileJson size={16} />;
  if (kind === "code") return <Code2 size={16} />;
  if (["pdf", "markdown", "text"].includes(kind)) return <FileText size={16} />;
  return <File size={16} />;
}

function selectionName(selection: Selection): string {
  return selection.type === "input" ? selection.value.name : selection.value.rel_path;
}

function rawPreviewUrl(taskId: string, selection: Selection): string {
  const base = `/api/tasks/${encodeURIComponent(taskId)}`;
  return selection.type === "input"
    ? `${base}/files/${encodeURIComponent(selection.value.id)}/preview`
    : `${base}/artifacts/${encodeURIComponent(selection.value.id)}/preview`;
}

function downloadUrl(taskId: string, selection: Selection): string {
  const base = `/api/tasks/${encodeURIComponent(taskId)}`;
  return selection.type === "input"
    ? `${base}/files/${encodeURIComponent(selection.value.id)}`
    : `${base}/artifacts/${encodeURIComponent(selection.value.id)}`;
}

function FilePreview({ taskId, selection }: { taskId: string; selection: Selection }) {
  const name = selectionName(selection);
  const mime = selection.value.mime;
  const kind = previewKind(name, mime);
  const url = rawPreviewUrl(taskId, selection);
  const [text, setText] = useState("");
  const [error, setError] = useState("");
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(() => ["json", "markdown", "code", "text"].includes(kind));
  const [version, setVersion] = useState(0);

  useEffect(() => {
    if (!["json", "markdown", "code", "text"].includes(kind)) return;
    const controller = new AbortController();
    fetch(url, { cache: "no-store", signal: controller.signal })
      .then(async (response) => {
        if (!response.ok) throw new Error(`Preview returned HTTP ${response.status}`);
        return readTextPreview(response);
      })
      .then((preview) => {
        setText(preview.text);
        setTruncated(preview.truncated);
      })
      .catch((caught: unknown) => {
        if (caught instanceof DOMException && caught.name === "AbortError") return;
        setError(caught instanceof Error ? caught.message : "The preview failed.");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [kind, url, version]);

  if (kind === "pdf") {
    return <iframe className="files-preview__frame" src={url} title={`Preview ${name}`} />;
  }
  if (kind === "image") {
    return (
      <div className="files-preview__image">
        <Image src={url} alt={`Preview of ${name}`} fill sizes="(max-width: 900px) 90vw, 60vw" unoptimized />
      </div>
    );
  }
  if (error) {
    return (
      <ActionableError
        component="File preview"
        cause={error}
        onRetry={() => {
          setLoading(true);
          setError("");
          setTruncated(false);
          setVersion((value) => value + 1);
        }}
        compact
      />
    );
  }
  if (loading) return <div className="files-preview__loading">Loading preview…</div>;
  if (kind === "json") {
    let formatted = text;
    try {
      formatted = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      formatted = text;
    }
    return (
      <>
        {truncated ? <div className="files-preview__notice">Preview limited to the first 1 MB.</div> : null}
        <pre className="files-preview__code"><code>{formatted}</code></pre>
      </>
    );
  }
  if (kind === "markdown") {
    return (
      <>
        {truncated ? <div className="files-preview__notice">Preview limited to the first 1 MB.</div> : null}
        <article className="files-preview__markdown">
          <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
        </article>
      </>
    );
  }
  if (kind === "code" || kind === "text") {
    return (
      <>
        {truncated ? <div className="files-preview__notice">Preview limited to the first 1 MB.</div> : null}
        <pre className="files-preview__code"><code>{text}</code></pre>
      </>
    );
  }
  return (
    <div className="files-preview__unsupported">
      <File size={32} />
      <p>This file type has no inline preview.</p>
      <a href={downloadUrl(taskId, selection)} download={name}>Download the file</a>
    </div>
  );
}

export function FilesWorkspace({
  taskId,
  liveFiles = EMPTY_FILES,
  liveArtifacts = EMPTY_ARTIFACTS,
}: {
  taskId: string;
  liveFiles?: readonly TaskFile[];
  liveArtifacts?: readonly TaskArtifact[];
}) {
  const [savedFiles, setSavedFiles] = useState<TaskFile[]>([]);
  const [savedArtifacts, setSavedArtifacts] = useState<TaskArtifact[]>([]);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loading, setLoading] = useState(true);
  const [loadVersion, setLoadVersion] = useState(0);

  const files = useMemo(
    () => mergeTaskFiles(savedFiles, liveFiles),
    [liveFiles, savedFiles],
  );
  const artifacts = useMemo(
    () => mergeTaskArtifacts(savedArtifacts, liveArtifacts),
    [liveArtifacts, savedArtifacts],
  );
  const artifactGroups = useMemo(() => groupArtifactVersions(artifacts), [artifacts]);

  useEffect(() => {
    const controller = new AbortController();
    Promise.all([
      fetch(`/api/tasks/${encodeURIComponent(taskId)}/files`, {
        cache: "no-store",
        signal: controller.signal,
      }),
      fetch(`/api/tasks/${encodeURIComponent(taskId)}/artifacts`, {
        cache: "no-store",
        signal: controller.signal,
      }),
    ])
      .then(async ([fileResponse, artifactResponse]) => {
        if (!fileResponse.ok) throw new Error(`Inputs returned HTTP ${fileResponse.status}`);
        if (!artifactResponse.ok && artifactResponse.status !== 404) {
          throw new Error(`Outputs returned HTTP ${artifactResponse.status}`);
        }
        const fileBody = await fileResponse.json();
        const artifactBody = artifactResponse.status === 404
          ? { artifacts: [] }
          : await artifactResponse.json();
        setSavedFiles(fileBody.files ?? []);
        setSavedArtifacts(artifactBody.artifacts ?? []);
        setLoadError("");
      })
      .catch((caught: unknown) => {
        if (caught instanceof DOMException && caught.name === "AbortError") return;
        setLoadError(caught instanceof Error ? caught.message : "The Files workspace failed to load.");
      })
      .finally(() => setLoading(false));
    return () => controller.abort();
  }, [loadVersion, taskId]);

  const activeSelection = selection
    ?? (files[0] ? { type: "input" as const, value: files[0] } : null)
    ?? (artifacts[0] ? { type: "output" as const, value: artifacts[0] } : null);

  const reload = () => {
    setLoading(true);
    setLoadError("");
    setLoadVersion((value) => value + 1);
  };

  if (loadError && files.length === 0 && artifacts.length === 0) {
    return (
      <ActionableError
        component="Files workspace"
        cause={loadError}
        onRetry={reload}
      />
    );
  }

  return (
    <div className="files-workspace">
      <header className="files-workspace__header">
        <div>
          <h2>Files</h2>
          <p>Review task inputs and agent outputs in one workspace.</p>
        </div>
        <button type="button" onClick={reload} disabled={loading}>
          <RefreshCw size={14} /> {loading ? "Refreshing…" : "Refresh"}
        </button>
      </header>

      {loadError ? (
        <ActionableError
          component="Files refresh"
          cause={loadError}
          onRetry={reload}
          compact
        />
      ) : null}

      <div className="files-workspace__grid">
        <aside className="files-list">
          <section>
            <h3>Inputs <span>{files.length}</span></h3>
            {files.length ? files.map((file) => (
              <button
                type="button"
                key={file.id}
                className={activeSelection?.type === "input" && activeSelection.value.id === file.id ? "is-selected" : ""}
                onClick={() => setSelection({ type: "input", value: file })}
              >
                <ItemIcon name={file.name} mime={file.mime} />
                <span><strong>{file.name}</strong><small>{formatBytes(file.bytes)}</small></span>
              </button>
            )) : <p className="files-list__empty">This task has no uploaded inputs.</p>}
          </section>

          <section>
            <h3>Outputs <span>{artifactGroups.size}</span></h3>
            {artifactGroups.size ? Array.from(artifactGroups.entries()).map(([path, versions]) => {
              const latest = versions[0];
              return (
                <button
                  type="button"
                  key={path}
                  className={activeSelection?.type === "output" && activeSelection.value.rel_path === path ? "is-selected" : ""}
                  onClick={() => setSelection({ type: "output", value: latest })}
                >
                  <ItemIcon name={path} mime={latest.mime} />
                  <span>
                    <strong>{path}</strong>
                    <small>{formatBytes(latest.bytes)} · {versions.length} version{versions.length === 1 ? "" : "s"}</small>
                  </span>
                </button>
              );
            }) : <p className="files-list__empty">Agent outputs appear here during the task.</p>}
          </section>
        </aside>

        <section className="files-detail" aria-label="Selected file details">
          {activeSelection ? (
            <FileDetail
              key={`${activeSelection.type}:${selectionName(activeSelection)}`}
              taskId={taskId}
              selection={activeSelection}
              versions={activeSelection.type === "output" ? artifactGroups.get(activeSelection.value.rel_path) ?? [] : []}
              onSelectVersion={(artifact) => setSelection({ type: "output", value: artifact })}
            />
          ) : (
            <div className="files-preview__unsupported">
              <File size={32} />
              <p>This task has no files.</p>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function FileDetail({
  taskId,
  selection,
  versions,
  onSelectVersion,
}: {
  taskId: string;
  selection: Selection;
  versions: TaskArtifact[];
  onSelectVersion: (artifact: TaskArtifact) => void;
}) {
  const name = selectionName(selection);
  const [compare, setCompare] = useState(versions.length > 1);
  const [leftId, setLeftId] = useState(versions[1]?.id ?? versions[0]?.id ?? "");
  const [rightId, setRightId] = useState(versions[0]?.id ?? "");
  const effectiveLeftId = versions.some((artifact) => artifact.id === leftId)
    ? leftId
    : versions[1]?.id ?? versions[0]?.id ?? "";
  const effectiveRightId = versions.some((artifact) => artifact.id === rightId)
    ? rightId
    : versions[0]?.id ?? "";
  const left = versions.find((artifact) => artifact.id === effectiveLeftId);
  const right = versions.find((artifact) => artifact.id === effectiveRightId);

  return (
    <>
      <header className="files-detail__header">
        <div>
          <h3>{name}</h3>
          <p>
            {selection.type === "input"
              ? `Input · ${formatBytes(selection.value.bytes)}`
              : `Output · version ${selection.value.version} · ${formatBytes(selection.value.bytes)}`}
          </p>
        </div>
        <div>
          {versions.length > 1 ? (
            <button type="button" onClick={() => setCompare((value) => !value)}>
              <GitCompare size={14} /> {compare ? "Single preview" : "Compare versions"}
            </button>
          ) : null}
          <a href={downloadUrl(taskId, selection)} download={name}>
            <Download size={14} /> Download
          </a>
        </div>
      </header>

      {versions.length ? (
        <div className="files-detail__versions">
          <History size={14} />
          {versions.map((artifact) => (
            <button
              type="button"
              key={artifact.id}
              className={selection.type === "output" && selection.value.id === artifact.id ? "is-selected" : ""}
              onClick={() => onSelectVersion(artifact)}
            >
              v{artifact.version}
              <small>{new Date(artifact.created_at).toLocaleString()}</small>
            </button>
          ))}
        </div>
      ) : null}

      {compare && left && right ? (
        <div className="files-compare">
          <div className="files-compare__toolbar">
            <Columns2 size={14} />
            <label>
              Left
              <Select value={effectiveLeftId} onChange={(event) => setLeftId(event.target.value)}>
                {versions.map((artifact) => <option key={artifact.id} value={artifact.id}>v{artifact.version}</option>)}
              </Select>
            </label>
            <label>
              Right
              <Select value={effectiveRightId} onChange={(event) => setRightId(event.target.value)}>
                {versions.map((artifact) => <option key={artifact.id} value={artifact.id}>v{artifact.version}</option>)}
              </Select>
            </label>
          </div>
          <div className="files-compare__panes">
            <div><FilePreview taskId={taskId} selection={{ type: "output", value: left }} /></div>
            <div><FilePreview taskId={taskId} selection={{ type: "output", value: right }} /></div>
          </div>
        </div>
      ) : (
        <div className="files-preview">
          <FilePreview key={`${selection.type}:${selection.value.id}`} taskId={taskId} selection={selection} />
        </div>
      )}
    </>
  );
}
