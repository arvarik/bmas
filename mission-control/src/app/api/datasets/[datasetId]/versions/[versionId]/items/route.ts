import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";
import { daemonFailure, daemonJsonResponse } from "@/lib/daemon-response";

export async function GET(
  request: Request,
  { params: routeParams }: { params: Promise<{ datasetId: string; versionId: string }> },
) {
  const { datasetId, versionId } = await routeParams;
  const incoming = new URL(request.url).searchParams;
  const params = new URLSearchParams();
  for (const key of ["search", "limit", "offset"]) {
    const value = incoming.get(key);
    if (value) params.set(key, value);
  }
  try {
    const response = await daemonFetch(
      `${DAEMON_BASE_URL}/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(versionId)}/items?${params}`,
      { cache: "no-store", signal: AbortSignal.timeout(10_000) },
    );
    return daemonJsonResponse(response);
  } catch (error) {
    return daemonFailure(error, "Dataset items are unavailable");
  }
}
