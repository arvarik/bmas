import { DAEMON_BASE_URL } from "@/lib/config";

// Force Node.js runtime (not Edge) for long-lived SSE streams.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  req: Request,
  { params }: { params: Promise<{ taskId: string }> },
) {
  const { taskId } = await params;

  // Mirror the client's disconnect signal to the upstream fetch.
  // When the browser tab closes, req.signal fires 'abort', which propagates
  // to the daemon's StreamingResponse, triggering Pub/Sub cleanup.
  const abortController = new AbortController();
  req.signal.addEventListener("abort", () => abortController.abort());

  try {
    const upstream = await fetch(`${DAEMON_BASE_URL}/events/${taskId}`, {
      signal: abortController.signal,
      cache: "no-store",
    });

    if (!upstream.ok || !upstream.body) {
      return new Response(
        JSON.stringify({ error: `Daemon returned ${upstream.status}` }),
        {
          status: upstream.status,
          headers: { "Content-Type": "application/json" },
        },
      );
    }

    return new Response(upstream.body, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache, no-transform",
        Connection: "keep-alive",
        "X-Accel-Buffering": "no", // Disable Caddy/nginx buffering
      },
    });
  } catch {
    return new Response(
      JSON.stringify({ error: "Daemon unreachable" }),
      { status: 503, headers: { "Content-Type": "application/json" } },
    );
  }
}
