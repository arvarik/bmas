import { DAEMON_BASE_URL } from "@/lib/config";
import {
  daemonFailure,
  daemonJsonResponse,
  daemonMutationHeaders,
} from "@/lib/daemon-response";

export interface DaemonProxyOptions {
  request?: Request;
  method?: "GET" | "POST";
}

/**
 * Forward one request to the daemon and return its JSON response.
 *
 * A mutation carries the daemon API key and the authenticated
 * operator identity; a read carries no credentials. Every failure
 * returns the same shape with the given unavailable message.
 */
export async function daemonProxy(
  path: string,
  options: DaemonProxyOptions,
  unavailableMessage: string,
) {
  const method = options.method ?? "GET";
  try {
    const body = method === "POST" && options.request
      ? await options.request.text()
      : undefined;
    // Every request carries the operator key; a mutation adds its body type.
    const headers: Record<string, string> = method === "POST"
      ? { "Content-Type": "application/json", ...daemonMutationHeaders() }
      : { ...daemonMutationHeaders() };
    const idempotencyKey = options.request?.headers.get("X-Idempotency-Key");
    if (idempotencyKey) headers["X-Idempotency-Key"] = idempotencyKey;
    const operatorId = options.request?.headers.get("X-BMAS-Operator-Id");
    if (operatorId) headers["X-Operator-Id"] = operatorId;
    const response = await fetch(`${DAEMON_BASE_URL}${path}`, {
      method,
      headers,
      body,
      cache: "no-store",
      signal: AbortSignal.timeout(method === "POST" ? 120_000 : 15_000),
    });
    return daemonJsonResponse(response);
  } catch (error) {
    return daemonFailure(error, unavailableMessage);
  }
}

export function benchmarkProxy(path: string, options: DaemonProxyOptions = {}) {
  return daemonProxy(path, options, "The benchmark service is unavailable");
}

export async function benchmarkRawProxy(path: string) {
  try {
    const response = await fetch(`${DAEMON_BASE_URL}${path}`, {
      cache: "no-store",
      headers: daemonMutationHeaders(),
      signal: AbortSignal.timeout(30_000),
    });
    const body = await response.arrayBuffer();
    return new Response(body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") ?? "application/octet-stream",
        "Content-Disposition": response.headers.get("Content-Disposition") ?? "attachment",
      },
    });
  } catch (error) {
    return daemonFailure(error, "The benchmark export is unavailable");
  }
}
