import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFailure, daemonJsonResponse } from "@/lib/daemon-response";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ datasetId: string }> },
) {
  const { datasetId } = await params;
  try {
    const response = await fetch(`${DAEMON_BASE_URL}/datasets/${encodeURIComponent(datasetId)}`, {
      cache: "no-store",
      signal: AbortSignal.timeout(10_000),
    });
    return daemonJsonResponse(response);
  } catch (error) {
    return daemonFailure(error, "Dataset detail is unavailable");
  }
}
