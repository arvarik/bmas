export type RequestFailureKind = "error" | "permission" | "unavailable";

export interface RequestFailure {
  kind: RequestFailureKind;
  message: string;
  detail: string;
  status?: number;
}

interface ErrorBody {
  error?: string;
  detail?: string;
  message?: string;
}

function kindForStatus(status: number): RequestFailureKind {
  if (status === 401 || status === 403) return "permission";
  if (status === 502 || status === 503 || status === 504) return "unavailable";
  return "error";
}

export async function failureFromResponse(
  response: Response,
  fallback: string,
): Promise<RequestFailure> {
  const body = await response.json().catch(() => ({})) as ErrorBody;
  const message = body.error ?? body.message ?? fallback;
  return {
    kind: kindForStatus(response.status),
    message,
    detail: body.detail ?? `${message} (HTTP ${response.status})`,
    status: response.status,
  };
}

export function failureFromReason(
  reason: unknown,
  fallback: string,
): RequestFailure {
  if (isRequestFailure(reason)) return reason;

  const message = reason instanceof Error ? reason.message : fallback;
  const normalized = message.toLowerCase();
  const permissionFailure = normalized.includes("unauthorized")
    || normalized.includes("forbidden")
    || normalized.includes("permission")
    || /\b40[13]\b/.test(normalized);
  const unavailableFailure = reason instanceof TypeError
    || normalized.includes("unreachable")
    || normalized.includes("failed to fetch")
    || normalized.includes("network")
    || normalized.includes("timeout")
    || /\b50[234]\b/.test(normalized);

  return {
    kind: permissionFailure
      ? "permission"
      : unavailableFailure
        ? "unavailable"
        : "error",
    message,
    detail: message,
  };
}

export function diagnosticsText(
  component: string,
  failure: RequestFailure,
  context: Record<string, unknown> = {},
): string {
  return JSON.stringify({
    component,
    state: failure.kind,
    status: failure.status ?? null,
    message: failure.message,
    detail: failure.detail,
    captured_at: new Date().toISOString(),
    ...context,
  }, null, 2);
}

function isRequestFailure(value: unknown): value is RequestFailure {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Partial<RequestFailure>;
  return (candidate.kind === "error"
      || candidate.kind === "permission"
      || candidate.kind === "unavailable")
    && typeof candidate.message === "string"
    && typeof candidate.detail === "string";
}
