import {
  dataList,
  hermesAgentErrorResponse,
  requestHermesAgent,
} from "@/lib/hermes-agent-api";

/** Return the resolved toolsets that the selected Hermes API server exposes. */
export async function GET(request: Request): Promise<Response> {
  const node = new URL(request.url).searchParams.get("node") ?? "";
  try {
    const body = await requestHermesAgent(node, "/v1/toolsets");
    const platform = typeof body === "object" && body !== null
      ? (body as Record<string, unknown>).platform
      : undefined;
    return Response.json(
      {
        toolsets: dataList(body),
        platform: typeof platform === "string" ? platform : "api_server",
        read_only: true,
      },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    return hermesAgentErrorResponse(error);
  }
}
