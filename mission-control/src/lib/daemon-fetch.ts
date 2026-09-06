/**
 * Credentials for every daemon request the dashboard makes on behalf
 * of an operator. The daemon authenticates reads as well as mutations
 * at its edge when an operator key is configured, so every proxied
 * fetch carries the key, and the authenticated operator identity when
 * the request has one.
 */
export function daemonHeaders(extra: Record<string, string> = {}, operatorId?: string | null): Record<string, string> {
  const headers: Record<string, string> = { ...extra };
  if (process.env.BMAS_API_KEY) headers.Authorization = `Bearer ${process.env.BMAS_API_KEY}`;
  if (operatorId) headers["X-Operator-Id"] = operatorId;
  return headers;
}

/** fetch() against the daemon with the operator credentials attached. */
export function daemonFetch(input: string, init: RequestInit = {}, operatorId?: string | null): Promise<Response> {
  const given = new Headers(init.headers ?? {});
  for (const [name, value] of Object.entries(daemonHeaders({}, operatorId))) {
    if (!given.has(name)) given.set(name, value);
  }
  return fetch(input, { ...init, headers: given });
}
