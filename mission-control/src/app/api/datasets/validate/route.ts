import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";
import { daemonFailure, daemonJsonResponse } from "@/lib/daemon-response";

export async function POST(request: Request) {
  try {
    const response = await daemonFetch(`${DAEMON_BASE_URL}/datasets/validate`, {
      method: "POST",
      body: await request.formData(),
      signal: AbortSignal.timeout(60_000),
    });
    return daemonJsonResponse(response);
  } catch (error) {
    return daemonFailure(error, "Dataset validation is unavailable");
  }
}
