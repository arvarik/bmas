"use client";

/**
 * SystemStatusPanel — the top-bar system button.
 *
 * The button shows one combined state: the daemon connection and the
 * readiness document. The popover shows the exact services, checks, and
 * credentials, plus the repair actions an operator needs.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { Copy, Play, RefreshCw, ServerCog, X } from "lucide-react";
import { useToast } from "@/hooks/useToast";
import { useReadiness } from "@/contexts/ReadinessContext";
import type {
  SystemConnectionState,
  SystemDependencyIssue,
} from "@/hooks/useSystemStream";
import styles from "./SystemStatusPanel.module.css";
import { useFocusTrap } from "@/hooks/useFocusTrap";

interface SystemStatusPanelProps {
  state: SystemConnectionState;
  isStale: boolean;
  failedDependencies: SystemDependencyIssue[];
  affectedFeatures: string[];
  onRetry: () => void;
}

type Tone = "ready" | "warning" | "error" | "pending";

const CONNECTION_PRESENTATION: Record<
  SystemConnectionState,
  { tone: Tone; label: string; summary: string }
> = {
  connecting: { tone: "pending", label: "Connecting", summary: "Mission Control waits for the first daemon health report." },
  ready: { tone: "ready", label: "System ready", summary: "The daemon and its reported dependencies are available." },
  degraded: { tone: "warning", label: "Degraded", summary: "One or more dependencies need attention." },
  reconnecting: { tone: "pending", label: "Reconnecting", summary: "Mission Control waits for a fresh daemon health report." },
  disconnected: { tone: "error", label: "Disconnected", summary: "Mission Control cannot confirm the daemon state." },
  offline: { tone: "error", label: "Offline", summary: "Live server data and server actions are unavailable." },
};

const TEST_TASK_REQUEST_TIMEOUT_MS = 15_000;

export function SystemStatusPanel({
  state,
  isStale,
  failedDependencies,
  affectedFeatures,
  onRetry,
}: SystemStatusPanelProps) {
  const [open, setOpen] = useState(false);
  const [testTaskLoading, setTestTaskLoading] = useState(false);
  const [testTaskError, setTestTaskError] = useState("");
  const wrapperRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const { toast } = useToast();
  const router = useRouter();
  const readiness = useReadiness();
  const connection = CONNECTION_PRESENTATION[state];

  const presentation = useMemo<{ tone: Tone; label: string; summary: string }>(() => {
    if (state !== "ready" && state !== "degraded") return connection;
    if (readiness.loading && !readiness.document) {
      return { tone: "pending", label: "Checking services", summary: "Mission Control checks the starter services." };
    }
    if (readiness.error) {
      return { tone: "error", label: "Status unavailable", summary: readiness.error };
    }
    if (readiness.ready && state === "ready") {
      return { tone: "ready", label: "System ready", summary: "All required services passed their checks." };
    }
    if (readiness.ready && state === "degraded") return connection;
    return { tone: "warning", label: "Setup needs attention", summary: "One or more required checks failed." };
  }, [connection, readiness.document, readiness.error, readiness.loading, readiness.ready, state]);

  const closePanel = useCallback(() => setOpen(false), []);

  useFocusTrap({
    active: open,
    containerRef: panelRef,
    initialFocusRef: closeRef,
    returnFocusRef: triggerRef,
    onEscape: closePanel,
  });

  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: PointerEvent) {
      if (!wrapperRef.current?.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [open]);

  const document_ = readiness.document;
  const onlineAgents = document_ ? Object.values(document_.agent_health).filter((agent) => agent.alive).length : 0;
  const totalAgents = document_ ? Object.keys(document_.agent_health).length : 0;
  const failedChecks = useMemo(
    () => document_?.checks.filter((check) => !check.ready) ?? [],
    [document_],
  );

  const diagnostics = useMemo(() => JSON.stringify({
    connection: state,
    stale: isStale,
    readiness: document_?.status ?? null,
    readiness_error: readiness.error || null,
    checked_at: readiness.checkedAt,
    failed_checks: failedChecks.map((check) => ({ id: check.id, detail: check.detail, fix: check.fix })),
    failed_dependencies: failedDependencies.map((issue) => ({
      id: issue.id,
      label: issue.label,
      detail: issue.detail,
      affected_features: issue.affectedFeatures,
      remediation: issue.remediation,
    })),
  }, null, 2), [document_?.status, failedChecks, failedDependencies, isStale, readiness.checkedAt, readiness.error, state]);

  const copyDiagnostics = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(diagnostics);
      toast({ type: "success", message: "System diagnostics copied." });
    } catch {
      toast({ type: "error", message: "Mission Control could not copy the diagnostics." });
    }
  }, [diagnostics, toast]);

  const checkAgain = useCallback(() => {
    onRetry();
    void readiness.refresh();
  }, [onRetry, readiness]);

  const runTestTask = useCallback(async () => {
    setTestTaskLoading(true);
    setTestTaskError("");
    try {
      const response = await fetch("/api/submit", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          task: "System test: return a short confirmation that the classic swarm can execute a task.",
          variant: "classic",
        }),
        signal: AbortSignal.timeout(TEST_TASK_REQUEST_TIMEOUT_MS),
      });
      const body = await response.json().catch(() => ({})) as { task_id?: string; error?: string; detail?: string };
      if (!response.ok || !body.task_id) {
        throw new Error(body.detail || body.error || `Test task returned HTTP ${response.status}`);
      }
      setOpen(false);
      router.push(`/task/${body.task_id}`);
    } catch (caught) {
      setTestTaskError(caught instanceof Error ? caught.message : "The test task failed.");
    } finally {
      setTestTaskLoading(false);
    }
  }, [router]);

  const checkedLabel = readiness.checkedAt
    ? `Checked ${new Date(readiness.checkedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
    : "Not checked yet";

  return (
    <div className={styles.wrapper} ref={wrapperRef}>
      <button
        ref={triggerRef}
        type="button"
        className={styles.trigger}
        data-tone={presentation.tone}
        aria-expanded={open}
        aria-controls="global-system-status"
        aria-haspopup="dialog"
        aria-label={`System status: ${presentation.label}`}
        onClick={() => setOpen((value) => !value)}
      >
        <span className={styles.dot} data-tone={presentation.tone} aria-hidden="true" />
        <span className={styles.triggerLabel}>{presentation.label}</span>
      </button>

      {open ? (
        <section
          ref={panelRef}
          id="global-system-status"
          className={styles.panel}
          role="dialog"
          aria-modal="false"
          aria-labelledby="global-system-status-title"
        >
          <div className={styles.header}>
            <div className={styles.heading}>
              <strong id="global-system-status-title">{presentation.label}</strong>
              <span>{presentation.summary}</span>
            </div>
            <button
              ref={closeRef}
              type="button"
              className={styles.close}
              aria-label="Close system status"
              onClick={closePanel}
            >
              <X size={17} aria-hidden="true" />
            </button>
          </div>

          {document_ ? (
            <dl className={styles.metrics}>
              <Metric label="Provider" ok={document_.litellm_connected} value={document_.litellm_connected ? "Online" : "Offline"} />
              <Metric label="Agents" ok={onlineAgents === totalAgents && totalAgents > 0} value={`${onlineAgents}/${totalAgents}`} />
              <Metric label="Redis" ok={document_.redis_connected} value={document_.redis_connected ? "Up" : "Down"} />
              <Metric label="Storage" ok={document_.storage.ready} optional={!document_.storage.enabled} value={document_.storage.enabled ? (document_.storage.ready ? "Writable" : "Read-only") : "Off"} />
              <Metric label="Queue" ok={document_.task_queue.queued_tasks < document_.task_queue.queue_capacity} value={`${document_.task_queue.active_tasks} active · ${document_.task_queue.queued_tasks} queued`} />
            </dl>
          ) : null}

          {failedDependencies.length > 0 ? (
            <ul className={styles.issues} aria-label="Failed dependencies">
              {failedDependencies.map((issue) => (
                <li key={issue.id} className={styles.issue}>
                  <strong>{issue.label}</strong>
                  <p>{issue.detail}</p>
                  {issue.affectedFeatures.length ? <small>Affected: {issue.affectedFeatures.join(", ")}</small> : null}
                  <code>{issue.remediation}</code>
                </li>
              ))}
            </ul>
          ) : null}

          {affectedFeatures.length > 0 ? (
            <ul className={styles.features} aria-label="Affected features">
              {affectedFeatures.map((feature) => (
                <li key={feature} className={styles.feature}>{feature}</li>
              ))}
            </ul>
          ) : null}

          {document_ ? (
            <ul className={styles.checks} aria-label="Readiness checks">
              {document_.checks.map((check) => (
                <li key={check.id} className={styles.check} data-ok={check.ready}>
                  <span className={styles.checkDot} aria-hidden="true" />
                  <div>
                    <span className={styles.checkLabel}>
                      {check.label}{check.blocking === false ? " · optional" : ""}
                    </span>
                    <span className={styles.checkDetail}>{check.detail}</span>
                    {!check.ready ? <code className={styles.checkFix}>{check.fix}</code> : null}
                  </div>
                </li>
              ))}
            </ul>
          ) : readiness.error ? (
            <p className={styles.summary}>{readiness.error}. Run <code>./scripts/bmas doctor</code> for exact checks.</p>
          ) : (
            <p className={styles.summary}>Checking the starter services…</p>
          )}

          {document_?.provider_credentials.length ? (
            <details className={styles.credentials}>
              <summary>Provider credentials ({document_.provider_credentials.filter((c) => c.configured).length}/{document_.provider_credentials.length} configured)</summary>
              <ul>
                {document_.provider_credentials.map((credential) => (
                  <li key={credential.alias}>
                    <span>{credential.alias} · {credential.provider}</span>
                    <code>{credential.env_var || "No key required"}</code>
                    <strong data-state={credential.configured ? "ready" : credential.required ? "failed" : "optional"}>
                      {credential.configured ? "Configured" : credential.required ? "Missing" : "Not selected"}
                    </strong>
                  </li>
                ))}
              </ul>
            </details>
          ) : null}

          {testTaskError ? <p className={styles.errorText} role="alert">{testTaskError}</p> : null}

          <div className={styles.footer}>
            <span className={styles.timestamp}>{checkedLabel}{isStale ? " · stale" : ""}</span>
            <div className={styles.actions}>
              <button type="button" className={styles.action} onClick={checkAgain} disabled={readiness.loading}>
                <RefreshCw size={14} aria-hidden="true" /> {readiness.loading ? "Checking…" : "Check again"}
              </button>
              <button type="button" className={styles.action} onClick={() => void runTestTask()} disabled={!readiness.ready || testTaskLoading}>
                <Play size={14} aria-hidden="true" /> {testTaskLoading ? "Starting…" : "Run test task"}
              </button>
              <Link className={styles.action} href="/infra" onClick={() => setOpen(false)}>
                <ServerCog size={14} aria-hidden="true" /> Operations
              </Link>
              <button type="button" className={styles.action} onClick={() => void copyDiagnostics()}>
                <Copy size={14} aria-hidden="true" /> Copy diagnostics
              </button>
            </div>
          </div>
        </section>
      ) : null}
    </div>
  );
}

function Metric({
  label,
  value,
  ok,
  optional = false,
}: {
  label: string;
  value: string;
  ok: boolean;
  optional?: boolean;
}) {
  return (
    <div className={styles.metric}>
      <dt>{label}</dt>
      <dd data-state={ok ? "ready" : optional ? "optional" : "failed"}>{value}</dd>
    </div>
  );
}
