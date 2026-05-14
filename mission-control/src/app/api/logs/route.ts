import { getRedis } from "@/lib/redis";
import { STREAM_KEYS } from "@/lib/config";
import type { RedisClientType } from "redis";

// Force Node.js runtime (not Edge) for long-lived SSE streams with blocking Redis reads.
export const runtime = "nodejs";
export const dynamic = "force-dynamic";

/** Parsed entry coming out of a Redis XREAD response. */
interface StreamEntry {
  id: string;
  message: Record<string, string>;
}

/** Shape returned by node-redis commandOptions-less XREAD. */
interface XReadResponse {
  name: string;
  messages: Array<{
    id: string;
    message: Record<string, string>;
  }>;
}

export async function GET(request: Request): Promise<Response> {
  // ── Resolve starting cursor ────────────────────────────────────────
  const lastEventId = request.headers.get("Last-Event-ID");

  // If the browser reconnects it sends Last-Event-ID.
  // We use it as the starting cursor for all streams so we resume
  // right after the last successfully-delivered message.
  // '$' means "only new messages from this point on."
  const startId = lastEventId ?? "$";

  // Track the latest ID we've seen per stream so subsequent XREAD
  // calls pick up only newer entries.
  const cursors: Record<string, string> = {};
  for (const key of STREAM_KEYS) {
    cursors[key] = startId;
  }

  const encoder = new TextEncoder();
  let subscriber: RedisClientType | null = null;

  const stream = new ReadableStream({
    async start(controller) {
      // Duplicate the shared client so we get our own blocking connection.
      const redis = await getRedis();
      subscriber = redis.duplicate() as RedisClientType;
      subscriber.on("error", (err: Error) => {
        console.error("[sse/logs] redis subscriber error:", err.message);
      });
      await subscriber.connect();

      try {
        while (true) {
          // Build the XREAD command arguments dynamically from cursors.
          const streams = STREAM_KEYS.map((key) => ({
            key,
            id: cursors[key],
          }));

          const results = (await subscriber.xRead(
            streams,
            { BLOCK: 5_000, COUNT: 100 },
          )) as XReadResponse[] | null;

          if (results === null) {
            // Timeout with no new messages — send a keep-alive comment
            // so proxies/browsers don't close the connection.
            controller.enqueue(encoder.encode(":keepalive\n\n"));
            continue;
          }

          for (const streamResult of results) {
            const streamName = streamResult.name;
            const agent = streamName.split(":").pop() ?? "unknown";

            for (const entry of streamResult.messages as StreamEntry[]) {
              cursors[streamName] = entry.id;

              const payload = JSON.stringify({
                stream: streamName,
                agent,
                ts: entry.id,
                // Map daemon's 'msg' field to 'line' for LogTerminal display
                line: entry.message.msg ?? entry.message.line ?? entry.message.text ?? "",
                level: entry.message.level,
                ...entry.message,
              });

              const frame = [
                `id: ${entry.id}`,
                `event: log`,
                `data: ${payload}`,
                "",
                "",
              ].join("\n");

              controller.enqueue(encoder.encode(frame));
            }
          }
        }
      } catch (err) {
        // If the client disconnected, this is expected.
        if (
          err instanceof Error &&
          !err.message.includes("Controller is already closed")
        ) {
          console.error("[sse/logs] stream error:", err.message);
        }
      } finally {
        await subscriber?.disconnect().catch(() => {});
        subscriber = null;
      }
    },

    cancel() {
      // Client disconnected — tear down the blocking Redis connection.
      subscriber?.disconnect().catch(() => {});
      subscriber = null;
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache, no-transform",
      Connection: "keep-alive",
      "X-Accel-Buffering": "no", // Disable nginx buffering if proxied
    },
  });
}
