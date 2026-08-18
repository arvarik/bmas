import { DAEMON_BASE_URL } from "@/lib/config";

// Force Node.js runtime (not Edge) for long-lived SSE streams.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

export async function GET(
  req: Request,
  { params }: { params: Promise<{ taskId: string }> },
) {
  const { taskId } = await params;
  if (!taskId || !/^[a-zA-Z0-9_-]{1,64}$/.test(taskId)) {
    return new Response(JSON.stringify({ error: "Invalid task ID" }), {
      status: 400,
      headers: { "Content-Type": "application/json" },
    });
  }

  // Mirror the client's disconnect signal to the upstream fetch.
  // When the browser tab closes, req.signal fires 'abort', which propagates
  // to the daemon's StreamingResponse, triggering Pub/Sub cleanup.
  const abortController = new AbortController();
  req.signal.addEventListener("abort", () => abortController.abort());

  try {
    const lastEventId = req.headers.get("last-event-id");
    const upstreamUrl = `${DAEMON_BASE_URL}/events/${encodeURIComponent(taskId)}`;
    const fetchUpstream = (cursor: string | null) => fetch(upstreamUrl, {
        signal: abortController.signal,
        cache: "no-store",
        headers: cursor ? { "Last-Event-ID": cursor } : undefined,
      });
    let upstream = await fetchUpstream(lastEventId);
    if (lastEventId && upstream.status === 409) {
      const conflict = await upstream.clone().json().catch(() => null) as { error?: unknown } | null;
      if (conflict?.error === "event_cursor_gap") {
        upstream = await fetchUpstream(null);
      }
    }

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
