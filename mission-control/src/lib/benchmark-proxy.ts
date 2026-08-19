import { DAEMON_BASE_URL } from "@/lib/config";
import {
  daemonFailure,
  daemonJsonResponse,
  daemonMutationHeaders,
} from "@/lib/daemon-response";

export async function benchmarkProxy(
  path: string,
  options: { request?: Request; method?: "GET" | "POST" } = {},
) {
  const method = options.method ?? "GET";
  try {
    const body = method === "POST" && options.request
      ? await options.request.text()
      : undefined;
    const headers: Record<string, string> = method === "POST"
      ? { "Content-Type": "application/json", ...daemonMutationHeaders() }
      : {};
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
    return daemonFailure(error, "The benchmark service is unavailable");
  }
}

export async function benchmarkRawProxy(path: string) {
  try {
    const response = await fetch(`${DAEMON_BASE_URL}${path}`, {
      cache: "no-store",
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
