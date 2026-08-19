import {
  dataList,
  hermesAgentErrorResponse,
  requestHermesAgent,
} from "@/lib/hermes-agent-api";

/** Return the skills that the selected Hermes API server exposes. */
export async function GET(request: Request): Promise<Response> {
  const node = new URL(request.url).searchParams.get("node") ?? "";
  try {
    const body = await requestHermesAgent(node, "/v1/skills");
    return Response.json(
      { skills: dataList(body), read_only: true },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    return hermesAgentErrorResponse(error);
  }
}
