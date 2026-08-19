"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Check, ChevronDown, ChevronUp, Play, RefreshCw, X } from "lucide-react";
import { ActionableError } from "@/components/ui/ActionableError";
import { ResourceState } from "@/components/ui/ResourceState";

export interface ReadinessCheck {
  id: string;
  label: string;
  ready: boolean;
  detail: string;
  fix: string;
  blocking?: boolean;
}

interface ProviderCredential {
  alias: string;
  provider: string;
  env_var: string;
  required: boolean;
  configured: boolean;
}

export interface ReadinessDocument {
  status: "ready" | "not_ready";
  checks: ReadinessCheck[];
  provider_credentials: ProviderCredential[];
  agent_health: Record<string, { alive: boolean; current_task?: string | null }>;
  storage: {
    enabled: boolean;
    uploads_writable: boolean;
    artifacts_writable: boolean;
    ready: boolean;
    max_upload_mb: number;
    max_output_mb: number;
  };
  task_queue: {
    queued_tasks: number;
    active_tasks: number;
    queue_capacity: number;
    active_capacity: number;
  };
  litellm_connected: boolean;
  redis_connected: boolean;
}

interface ReadinessPanelProps {
  onReadyChange: (ready: boolean) => void;
  showReadyGuide?: boolean;
}

const READINESS_REQUEST_TIMEOUT_MS = 10_000;
const TEST_TASK_REQUEST_TIMEOUT_MS = 15_000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function parseReadiness(value: unknown): ReadinessDocument {
  if (!isRecord(value) || (value.status !== "ready" && value.status !== "not_ready")) {
    throw new Error("The daemon returned an invalid readiness status.");
  }
  if (!Array.isArray(value.checks)) {
    throw new Error("The daemon returned an invalid readiness checklist.");
  }
  const checks = value.checks.map((check): ReadinessCheck => {
    if (
      !isRecord(check)
      || typeof check.id !== "string"
      || typeof check.label !== "string"
      || typeof check.ready !== "boolean"
      || typeof check.detail !== "string"
      || typeof check.fix !== "string"
    ) {
      throw new Error("The daemon returned an invalid readiness check.");
    }
    return {
      id: check.id,
      label: check.label,
      ready: check.ready,
      detail: check.detail,
      fix: check.fix,
      blocking: typeof check.blocking === "boolean" ? check.blocking : true,
    };
  });
  return {
    status: value.status,
    checks,
    provider_credentials: Array.isArray(value.provider_credentials)
      ? value.provider_credentials as ProviderCredential[]
      : [],
    agent_health: isRecord(value.agent_health)
      ? value.agent_health as ReadinessDocument["agent_health"]
      : {},
    storage: isRecord(value.storage)
      ? value.storage as unknown as ReadinessDocument["storage"]
      : {
          enabled: false,
          uploads_writable: false,
          artifacts_writable: false,
          ready: false,
          max_upload_mb: 0,
          max_output_mb: 0,
        },
    task_queue: isRecord(value.task_queue)
      ? value.task_queue as unknown as ReadinessDocument["task_queue"]
      : { queued_tasks: 0, active_tasks: 0, queue_capacity: 0, active_capacity: 0 },
    litellm_connected: value.litellm_connected === true,
    redis_connected: value.redis_connected === true,
  };
}

async function requestReadiness(): Promise<ReadinessDocument> {
  const response = await fetch("/api/readiness", {
    cache: "no-store",
    signal: AbortSignal.timeout(READINESS_REQUEST_TIMEOUT_MS),
  });
  const raw: unknown = await response.json();
  if (!response.ok) {
    const message = isRecord(raw) && typeof raw.error === "string"
      ? raw.error
      : `Readiness returned HTTP ${response.status}`;
    throw new Error(message);
  }
  return parseReadiness(raw);
}

export function ReadinessPanel({
  onReadyChange,
  showReadyGuide = false,
}: ReadinessPanelProps) {
  const [document, setDocument] = useState<ReadinessDocument | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(true);
  const [testTaskLoading, setTestTaskLoading] = useState(false);
  const [testTaskError, setTestTaskError] = useState("");
  const router = useRouter();

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    onReadyChange(false);
    try {
      const next = await requestReadiness();
      setDocument(next);
      onReadyChange(next.status === "ready");
    } catch (caught) {
      setDocument(null);
      setError(caught instanceof Error ? caught.message : "Readiness is unavailable.");
    } finally {
      setLoading(false);
    }
  }, [onReadyChange]);

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
      const body = await response.json().catch(() => ({})) as {
        task_id?: string;
        error?: string;
        detail?: string;
      };
      if (!response.ok || !body.task_id) {
        throw new Error(body.detail || body.error || `Test task returned HTTP ${response.status}`);
      }
      router.push(`/task/${body.task_id}`);
    } catch (caught) {
      setTestTaskError(caught instanceof Error ? caught.message : "The test task failed.");
    } finally {
      setTestTaskLoading(false);
    }
  }, [router]);

  useEffect(() => {
    let cancelled = false;
    requestReadiness()
      .then((next) => {
        if (cancelled) return;
        setDocument(next);
        setLoading(false);
        onReadyChange(next.status === "ready");
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        setError(caught instanceof Error ? caught.message : "Readiness is unavailable.");
        setLoading(false);
        onReadyChange(false);
      });
    return () => {
      cancelled = true;
    };
  }, [onReadyChange]);

  if (loading) {
    return (
      <section className="readiness readiness--loading" aria-live="polite">
        <span className="readiness__dot readiness__dot--loading" />
        Checking the starter services…
      </section>
    );
  }

  if (error) {
    return (
      <ResourceState
        kind="unavailable"
        title="Task submission is unavailable"
        description="Mission Control cannot verify the services required for a new task."
        detail={`${error}\nRun ./scripts/bmas doctor for exact checks.`}
        diagnostics={JSON.stringify({
          component: "Task readiness",
          state: "unavailable",
          detail: error,
          captured_at: new Date().toISOString(),
        }, null, 2)}
        onRetry={() => void load()}
        operationsHref="/infra"
        compact
      />
    );
  }

  if (!document) return null;

  const ready = document.status === "ready";
  const onlineAgents = Object.values(document.agent_health).filter((agent) => agent.alive).length;
  const totalAgents = Object.keys(document.agent_health).length;

  return (
    <section className={`setup-center ${ready ? "setup-center--ready" : "setup-center--failed"}`} aria-live="polite">
      <button
        type="button"
        className="setup-center__summary"
        onClick={() => setExpanded((value) => !value)}
        aria-expanded={expanded}
      >
        <span className={`setup-center__status ${ready ? "setup-center__status--ready" : "setup-center__status--failed"}`}>
          {ready ? <Check size={15} /> : <X size={15} />}
        </span>
        <span>
          <strong>{ready ? "The classic starter is ready" : "Setup needs attention"}</strong>
          <small>{ready ? "All required services passed." : "Open the setup center for exact fixes."}</small>
        </span>
        {expanded ? <ChevronUp size={16} /> : <ChevronDown size={16} />}
      </button>

      {expanded ? (
        <div className="setup-center__body">
          {showReadyGuide ? (
            <p className="setup-center__guide">
              Check the stack, run one test task, then submit your own task below.
            </p>
          ) : null}
          <div className="setup-center__metrics">
            <SetupMetric label="Provider" ready={document.litellm_connected} value={document.litellm_connected ? "Gateway online" : "Gateway offline"} />
            <SetupMetric label="Agents" ready={onlineAgents === totalAgents && totalAgents > 0} value={`${onlineAgents}/${totalAgents} online`} />
            <SetupMetric label="Storage" ready={document.storage.ready} value={document.storage.ready ? "Writable" : "Unavailable"} optional={!document.storage.enabled} />
            <SetupMetric label="Redis" ready={document.redis_connected} value={document.redis_connected ? "Connected" : "Disconnected"} />
            <SetupMetric label="Queue" ready={document.task_queue.queued_tasks < document.task_queue.queue_capacity} value={`${document.task_queue.active_tasks}/${document.task_queue.active_capacity} active, ${document.task_queue.queued_tasks}/${document.task_queue.queue_capacity} queued`} />
          </div>

          {document.provider_credentials.length ? (
            <div className="setup-center__credentials">
              <h4>Provider credentials</h4>
              {document.provider_credentials.map((credential) => (
                <div key={credential.alias}>
                  <span>{credential.alias} · {credential.provider}</span>
                  <code>{credential.env_var || "No key required"}</code>
                  <strong className={credential.configured ? "is-ready" : credential.required ? "is-failed" : "is-optional"}>
                    {credential.configured
                      ? "Configured"
                      : credential.required ? "Missing" : "Not selected"}
                  </strong>
                </div>
              ))}
            </div>
          ) : null}

          <ul className="readiness__checks">
            {document.checks.map((check) => (
              <li key={check.id} className="readiness__check">
                <span className={`readiness__dot ${check.ready ? "readiness__dot--ready" : "readiness__dot--failed"}`} />
                <div>
                  <span className="readiness__check-label">
                    {check.label}{check.blocking === false ? " · optional" : ""}
                  </span>
                  <span className="readiness__check-detail">{check.detail}</span>
                  {!check.ready ? <code className="readiness__fix">{check.fix}</code> : null}
                </div>
              </li>
            ))}
          </ul>

          {testTaskError ? (
            <ActionableError
              component="Test task"
              cause={testTaskError}
              onRetry={() => void runTestTask()}
              compact
            />
          ) : null}

          <div className="setup-center__actions">
            <button type="button" onClick={() => void load()}>
              <RefreshCw size={14} /> Check again
            </button>
            <button type="button" onClick={() => void runTestTask()} disabled={!ready || testTaskLoading}>
              <Play size={14} /> {testTaskLoading ? "Starting test…" : "Run one test task"}
            </button>
          </div>
        </div>
      ) : null}
    </section>
  );
}

function SetupMetric({
  label,
  value,
  ready,
  optional = false,
}: {
  label: string;
  value: string;
  ready: boolean;
  optional?: boolean;
}) {
  return (
    <div className="setup-center__metric">
      <span>{label}</span>
      <strong className={ready ? "is-ready" : optional ? "is-optional" : "is-failed"}>
        {value}
      </strong>
    </div>
  );
}
