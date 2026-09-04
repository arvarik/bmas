import { daemonProxy, type DaemonProxyOptions } from "@/lib/benchmark-proxy";

/** Forward one request to the daemon evaluation API. */
export function evaluationProxy(path: string, options: DaemonProxyOptions = {}) {
  return daemonProxy(`/api/evaluation${path}`, options, "The evaluation service is unavailable");
}
