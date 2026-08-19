import {
  hermesAgentErrorResponse,
  requestHermesAgent,
} from "@/lib/hermes-agent-api";

interface RouteContext {
  params: Promise<{ sessionId: string }>;
}

/** Return session metadata and its latest messages. */
export async function GET(request: Request, context: RouteContext): Promise<Response> {
  const { sessionId } = await context.params;
  const node = new URL(request.url).searchParams.get("node") ?? "";
  const encodedId = encodeURIComponent(sessionId);

  try {
    const [sessionBody, messageBody] = await Promise.all([
      requestHermesAgent(node, `/api/sessions/${encodedId}`),
      requestHermesAgent(
        node,
        `/api/sessions/${encodedId}/messages?limit=200&order=latest`,
      ),
    ]);
    const session = typeof sessionBody === "object" && sessionBody !== null
      ? (sessionBody as Record<string, unknown>).session ?? sessionBody
      : sessionBody;
    const messages = typeof messageBody === "object" && messageBody !== null
      ? (messageBody as Record<string, unknown>).data ?? []
      : [];
    return Response.json(
      { session, messages },
      { headers: { "Cache-Control": "no-store" } },
    );
  } catch (error) {
    return hermesAgentErrorResponse(error);
  }
}
