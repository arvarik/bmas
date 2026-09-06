import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";
import { daemonFailure, daemonJsonResponse } from "@/lib/daemon-response";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ taskId: string }> },
) {
  const { taskId } = await params;
  const limit = new URL(request.url).searchParams.get("limit") ?? "200";
  try {
    const response = await daemonFetch(
      `${DAEMON_BASE_URL}/tasks/${encodeURIComponent(taskId)}/operator-actions?limit=${encodeURIComponent(limit)}`,
      { cache: "no-store", signal: AbortSignal.timeout(8_000) },
    );
    return daemonJsonResponse(response);
  } catch (error) {
    return daemonFailure(error, "Operator action history is unavailable");
  }
}
