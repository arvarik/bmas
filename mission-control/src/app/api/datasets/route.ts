import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";
import { daemonFailure, daemonJsonResponse } from "@/lib/daemon-response";

export async function GET(request: Request) {
  const incoming = new URL(request.url).searchParams;
  const params = new URLSearchParams();
  for (const key of ["search", "limit", "offset"]) {
    const value = incoming.get(key);
    if (value) params.set(key, value);
  }
  try {
    const response = await daemonFetch(`${DAEMON_BASE_URL}/datasets?${params}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return daemonJsonResponse(response);
  } catch (error) {
    return daemonFailure(error, "Dataset registry is unavailable");
  }
}
