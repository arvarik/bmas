/**
 * Readiness contract — the daemon's /readiness document.
 *
 * Mission Control reads this document once per shell session and after an
 * operator asks for a fresh check. The top-bar system button and the task
 * composer both consume it through ReadinessContext.
 */

export interface ReadinessCheck {
  id: string;
  label: string;
  ready: boolean;
  detail: string;
  fix: string;
  blocking?: boolean;
}

export interface ProviderCredential {
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

export const READINESS_REQUEST_TIMEOUT_MS = 10_000;

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

export async function requestReadiness(signal?: AbortSignal): Promise<ReadinessDocument> {
  const response = await fetch("/api/readiness", {
    cache: "no-store",
    signal: signal ?? AbortSignal.timeout(READINESS_REQUEST_TIMEOUT_MS),
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
