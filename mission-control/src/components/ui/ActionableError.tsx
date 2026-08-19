"use client";

import { AlertTriangle, ExternalLink, RotateCcw } from "lucide-react";

const DEFAULT_DOCS_URL =
  "https://github.com/arvarik/bmas/blob/main/docs/OPERATIONS.md";

export function ActionableError({
  component,
  cause,
  timestamp = new Date().toISOString(),
  onRetry,
  docsUrl = DEFAULT_DOCS_URL,
  compact = false,
}: {
  component: string;
  cause: string;
  timestamp?: string;
  onRetry?: () => void;
  docsUrl?: string;
  compact?: boolean;
}) {
  return (
    <div className={`actionable-error ${compact ? "actionable-error--compact" : ""}`} role="alert">
      <AlertTriangle size={18} aria-hidden="true" />
      <div className="actionable-error__body">
        <strong>{component} failed</strong>
        <p>{cause}</p>
        <time dateTime={timestamp}>{new Date(timestamp).toLocaleString()}</time>
      </div>
      <div className="actionable-error__actions">
        {onRetry ? (
          <button type="button" onClick={onRetry}>
            <RotateCcw size={13} /> Retry
          </button>
        ) : null}
        <a href={docsUrl} target="_blank" rel="noreferrer">
          Documentation <ExternalLink size={12} />
        </a>
      </div>
    </div>
  );
}
