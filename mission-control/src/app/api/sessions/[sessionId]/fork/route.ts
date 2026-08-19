import {
  hermesAgentErrorResponse,
  requestHermesAgent,
} from "@/lib/hermes-agent-api";

interface RouteContext {
  params: Promise<{ sessionId: string }>;
}

interface ForkBody {
  node?: string;
  title?: string;
  id?: string;
}

/** Fork a Hermes session while preserving its transcript and lineage. */
export async function POST(request: Request, context: RouteContext): Promise<Response> {
  let body: ForkBody;
  try {
    body = await request.json() as ForkBody;
  } catch {
    return Response.json({ error: "The request body must contain JSON" }, { status: 400 });
  }

  const node = body.node?.trim() ?? "";
  const title = body.title?.trim();
  const id = body.id?.trim();
  if (title && title.length > 200) {
    return Response.json({ error: "The title must contain at most 200 characters" }, { status: 400 });
  }
  if (id && !/^[a-zA-Z0-9_.-]{1,256}$/.test(id)) {
    return Response.json({ error: "The session ID contains unsupported characters" }, { status: 400 });
  }

  const { sessionId } = await context.params;
  try {
    const result = await requestHermesAgent(
      node,
      `/api/sessions/${encodeURIComponent(sessionId)}/fork`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...(title ? { title } : {}),
          ...(id ? { id } : {}),
        }),
      },
    );
    return Response.json(result, {
      status: 201,
      headers: { "Cache-Control": "no-store" },
    });
  } catch (error) {
    return hermesAgentErrorResponse(error);
  }
}
