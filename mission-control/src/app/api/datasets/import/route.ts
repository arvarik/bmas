import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFetch } from "@/lib/daemon-fetch";
import {
  daemonFailure,
  daemonJsonResponse,
  daemonMutationHeaders,
} from "@/lib/daemon-response";

export async function POST(request: Request) {
  try {
    const response = await daemonFetch(`${DAEMON_BASE_URL}/datasets/import`, {
      method: "POST",
      headers: daemonMutationHeaders(),
      body: await request.formData(),
      signal: AbortSignal.timeout(120_000),
    });
    return daemonJsonResponse(response);
  } catch (error) {
    return daemonFailure(error, "Dataset import is unavailable");
  }
}
