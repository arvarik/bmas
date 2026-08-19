"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { Copy, RefreshCw, ServerCog, X } from "lucide-react";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { useToast } from "@/hooks/useToast";
import type { StatusType } from "@/lib/design-tokens";
import type {
  SystemConnectionState,
  SystemDependencyIssue,
} from "@/hooks/useSystemStream";
import styles from "./SystemStatusPanel.module.css";

interface SystemStatusPanelProps {
  state: SystemConnectionState;
  lastSuccessfulEventAt: string | null;
  isStale: boolean;
  failedDependencies: SystemDependencyIssue[];
  affectedFeatures: string[];
  onRetry: () => void;
}

const STATE_PRESENTATION: Record<
  SystemConnectionState,
  { badge: StatusType; label: string; title: string; summary: string }
> = {
  connecting: {
    badge: "pending",
    label: "Connecting",
    title: "Connecting to the daemon",
    summary: "Mission Control waits for the first daemon health report.",
  },
  ready: {
    badge: "success",
    label: "Ready",
    title: "System ready",
    summary: "The daemon and its reported dependencies are available.",
  },
  degraded: {
    badge: "paused",
    label: "Degraded",
    title: "System degraded",
    summary: "One or more dependencies need attention.",
  },
  reconnecting: {
    badge: "running",
    label: "Reconnecting",
    title: "Reconnecting live updates",
    summary: "Mission Control waits for a fresh daemon health report.",
  },
  disconnected: {
    badge: "error",
    label: "Disconnected",
    title: "Daemon disconnected",
    summary: "Mission Control cannot confirm the daemon state.",
  },
  offline: {
    badge: "error",
    label: "Offline",
    title: "Browser offline",
    summary: "Live server data and server actions are unavailable.",
  },
};

function formatEventTime(value: string | null): string {
  if (!value) return "No successful system event received";
  return `Last successful event: ${new Date(value).toLocaleString()}`;
}

function compactEventTime(value: string | null): string {
  if (!value) return "No events";
  return `Last event ${new Date(value).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })}`;
}

export function SystemStatusPanel({
  state,
  lastSuccessfulEventAt,
  isStale,
  failedDependencies,
  affectedFeatures,
  onRetry,
}: SystemStatusPanelProps) {
  const [open, setOpen] = useState(false);
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const { toast } = useToast();
  const presentation = STATE_PRESENTATION[state];
  const timestamp = formatEventTime(lastSuccessfulEventAt);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();

    function handlePointerDown(event: PointerEvent) {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.stopPropagation();
      setOpen(false);
      triggerRef.current?.focus();
    }

    document.addEventListener("pointerdown", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown, true);
    };
  }, [open]);

  const diagnostics = useMemo(() => JSON.stringify({
    state,
    stale: isStale,
    last_successful_event_at: lastSuccessfulEventAt,
    failed_dependencies: failedDependencies.map((issue) => ({
      id: issue.id,
      label: issue.label,
      detail: issue.detail,
      affected_features: issue.affectedFeatures,
      remediation: issue.remediation,
    })),
  }, null, 2), [failedDependencies, isStale, lastSuccessfulEventAt, state]);

  const copyDiagnostics = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(diagnostics);
      toast({ type: "success", message: "System diagnostics copied." });
    } catch {
      toast({ type: "error", message: "Mission Control could not copy the diagnostics." });
    }
  }, [diagnostics, toast]);

  return (
    <div className={styles.wrapper} ref={wrapperRef}>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        aria-expanded={open}
        aria-controls="global-system-status"
        aria-haspopup="dialog"
        aria-label={`System status: ${presentation.label}. ${compactEventTime(lastSuccessfulEventAt)}.`}
        onClick={() => setOpen((value) => !value)}
      >
        <StatusBadge status={presentation.badge} label={presentation.label} />
        <span className={styles.lastEvent}>{compactEventTime(lastSuccessfulEventAt)}</span>
      </button>

      {open ? (
        <section
          id="global-system-status"
          className={styles.panel}
          role="dialog"
          aria-modal="false"
          aria-labelledby="global-system-status-title"
        >
          <div className={styles.header}>
            <div className={styles.heading}>
              <strong id="global-system-status-title">{presentation.title}</strong>
              <span>{presentation.summary}</span>
            </div>
            <button
              ref={closeRef}
              type="button"
              className={styles.close}
              aria-label="Close system status"
              onClick={() => {
                setOpen(false);
                triggerRef.current?.focus();
              }}
            >
              <X size={17} aria-hidden="true" />
            </button>
          </div>

          <div className={styles.timestamp}>{timestamp}{isStale ? " · stale" : ""}</div>

          {failedDependencies.length > 0 ? (
            <ul className={styles.issues} aria-label="Failed dependencies">
              {failedDependencies.map((issue) => (
                <li key={issue.id} className={styles.issue}>
                  <strong>{issue.label}</strong>
                  <p>{issue.detail}</p>
                  <small>Affected: {issue.affectedFeatures.join(", ")}</small>
                  <code>{issue.remediation}</code>
                </li>
              ))}
            </ul>
          ) : (
            <p className={styles.summary}>
              {state === "ready"
                ? "Live task updates, operator actions, and task history are available."
                : "Mission Control does not have dependency details yet."}
            </p>
          )}

          {affectedFeatures.length > 0 ? (
            <ul className={styles.features} aria-label="Affected features">
              {affectedFeatures.map((feature) => (
                <li key={feature} className={styles.feature}>{feature}</li>
              ))}
            </ul>
          ) : null}

          <div className={styles.actions}>
            <button type="button" className={styles.action} onClick={onRetry}>
              <RefreshCw size={14} aria-hidden="true" /> Retry
            </button>
            <Link className={styles.action} href="/infra" onClick={() => setOpen(false)}>
              <ServerCog size={14} aria-hidden="true" /> Open operations
            </Link>
            <button type="button" className={styles.action} onClick={() => void copyDiagnostics()}>
              <Copy size={14} aria-hidden="true" /> Copy diagnostics
            </button>
          </div>
        </section>
      ) : null}
    </div>
  );
}
