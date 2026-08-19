import {
  hermesAgentErrorResponse,
  requestHermesAgent,
} from "@/lib/hermes-agent-api";

const SAFE_QUERY_KEYS = ["limit", "offset", "source", "include_children"] as const;

/** List Hermes sessions for one configured agent node. */
export async function GET(request: Request): Promise<Response> {
  const sourceUrl = new URL(request.url);
  const node = sourceUrl.searchParams.get("node") ?? "";
  const query = new URLSearchParams();
  for (const key of SAFE_QUERY_KEYS) {
    const value = sourceUrl.searchParams.get(key);
    if (value !== null) query.set(key, value);
  }
  const suffix = query.size ? `?${query.toString()}` : "";

  try {
    const body = await requestHermesAgent(node, `/api/sessions${suffix}`);
    return Response.json(body, { headers: { "Cache-Control": "no-store" } });
  } catch (error) {
    return hermesAgentErrorResponse(error);
  }
}
