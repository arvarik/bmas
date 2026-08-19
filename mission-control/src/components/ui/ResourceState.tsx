"use client";

import Link from "next/link";
import {
  AlertTriangle,
  Clipboard,
  ClipboardCheck,
  RefreshCw,
  SearchX,
  ShieldAlert,
  WifiOff,
} from "lucide-react";
import { useState } from "react";
import type { RequestFailureKind } from "@/lib/request-state";

type ResourceStateKind = RequestFailureKind | "empty";

const ICONS = {
  empty: SearchX,
  unavailable: WifiOff,
  permission: ShieldAlert,
  error: AlertTriangle,
} as const;

export function ResourceState({
  kind,
  title,
  description,
  detail,
  diagnostics,
  onRetry,
  operationsHref,
  compact = false,
}: {
  kind: ResourceStateKind;
  title: string;
  description: string;
  detail?: string;
  diagnostics?: string;
  onRetry?: () => void | Promise<void>;
  operationsHref?: string;
  compact?: boolean;
}) {
  const Icon = ICONS[kind];
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const [retrying, setRetrying] = useState(false);
  const announcesError = kind === "error" || kind === "permission";

  const copy = async () => {
    if (!diagnostics) return;
    try {
      await navigator.clipboard.writeText(diagnostics);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
  };

  const retry = async () => {
    if (!onRetry || retrying) return;
    setRetrying(true);
    try {
      await onRetry();
    } finally {
      setRetrying(false);
    }
  };

  return (
    <section
      className={`resource-state resource-state--${kind} ${compact ? "resource-state--compact" : ""}`}
      aria-live={announcesError ? "assertive" : "polite"}
    >
      <Icon className="resource-state__icon" size={compact ? 24 : 34} aria-hidden="true" />
      <div className="resource-state__content">
        <h3>{title}</h3>
        <p>{description}</p>
        {detail ? (
          <details className="resource-state__details">
            <summary>Technical details</summary>
            <code>{detail}</code>
          </details>
        ) : null}
        {onRetry || operationsHref || diagnostics ? (
          <div className="resource-state__actions">
            {onRetry ? (
              <button type="button" onClick={() => void retry()} disabled={retrying}>
                <RefreshCw className={retrying ? "spin" : undefined} size={13} aria-hidden="true" />
                {retrying ? "Retrying" : "Retry"}
              </button>
            ) : null}
            {operationsHref ? <Link href={operationsHref}>Open operations</Link> : null}
            {diagnostics ? (
              <button type="button" onClick={() => void copy()}>
                {copyState === "copied"
                  ? <ClipboardCheck size={13} aria-hidden="true" />
                  : <Clipboard size={13} aria-hidden="true" />}
                {copyState === "copied" ? "Copied" : "Copy diagnostics"}
              </button>
            ) : null}
          </div>
        ) : null}
        {copyState === "failed" ? (
          <span className="resource-state__copy-result" role="status">
            The browser blocked clipboard access.
          </span>
        ) : null}
      </div>
    </section>
  );
}

export default ResourceState;
