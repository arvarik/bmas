import { NextResponse } from "next/server";
import { getRedis } from "@/lib/redis";

/** Allowed HITL actions. */
type HitlAction = "pause" | "resume" | "inject-hint";

/** POST request body schema. */
interface HitlPayload {
  action: HitlAction;
  task_id?: string;
  hint_text?: string;
}

const STATE_KEY = "bmas:public:state";

/** GET — return current pause state. */
export async function GET(): Promise<NextResponse> {
  try {
    const redis = await getRedis();
    const paused = await redis.hGet(STATE_KEY, "pause");

    return NextResponse.json({
      paused: paused === "true",
    });
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown Redis error";
    return NextResponse.json(
      { error: "Failed to read HITL state", detail: message },
      { status: 500 },
    );
  }
}

/** POST — pause, resume, or inject a hint. */
export async function POST(request: Request): Promise<NextResponse> {
  try {
    const body = (await request.json()) as HitlPayload;

    if (!body.action) {
      return NextResponse.json(
        { error: "Missing required field: action" },
        { status: 400 },
      );
    }

    const redis = await getRedis();

    switch (body.action) {
      case "pause": {
        await redis.hSet(STATE_KEY, "pause", "true");
        return NextResponse.json({ ok: true, paused: true });
      }

      case "resume": {
        await redis.hDel(STATE_KEY, "pause");
        return NextResponse.json({ ok: true, paused: false });
      }

      case "inject-hint": {
        if (!body.task_id || !body.hint_text) {
          return NextResponse.json(
            {
              error:
                "inject-hint requires both 'task_id' and 'hint_text' fields",
            },
            { status: 400 },
          );
        }

        const hintsKey = `bmas:public:hints:${body.task_id}`;
        await redis.lPush(hintsKey, body.hint_text);

        return NextResponse.json({
          ok: true,
          hints_key: hintsKey,
          hint_text: body.hint_text,
        });
      }

      default: {
        return NextResponse.json(
          {
            error: `Unknown action: '${body.action as string}'. Expected: pause | resume | inject-hint`,
          },
          { status: 400 },
        );
      }
    }
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown Redis error";
    return NextResponse.json(
      { error: "HITL operation failed", detail: message },
      { status: 500 },
    );
  }
}
