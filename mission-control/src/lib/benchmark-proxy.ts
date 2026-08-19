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
