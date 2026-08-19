import { NextResponse } from "next/server";
import { DAEMON_BASE_URL } from "@/lib/config";
import { getRedis } from "@/lib/redis";

type HitlAction =
  | "pause"
  | "resume"
  | "abort"
  | "inject-hint"
  | "boost"
  | "retract"
  | "approval"
  | "run-steer";

type ApprovalChoice = "once" | "session" | "always" | "deny";
const APPROVAL_CHOICES = new Set<ApprovalChoice>(["once", "session", "always", "deny"]);

interface HitlPayload {
  action: HitlAction;
  task_id?: string;
  hint_text?: string;
  reason?: string;
  run_id?: string;
  choice?: ApprovalChoice;
  input?: string;
  entry_id?: string;
}

function daemonHeaders(): Record<string, string> {
  return {
    "Content-Type": "application/json",
    ...(process.env.BMAS_API_KEY
      ? { Authorization: `Bearer ${process.env.BMAS_API_KEY}` }
      : {}),
  };
}

export async function GET(request: Request): Promise<NextResponse> {
  try {
    const taskId = new URL(request.url).searchParams.get("task_id");
    if (!taskId) {
      return NextResponse.json({ paused: false });
    }
    const redis = await getRedis();
    const paused = await redis.get(`bmas:public:pause:${taskId}`);
    return NextResponse.json({ paused: paused !== null });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown Redis error";
    return NextResponse.json(
      { error: "Failed to read task pause state", detail: message },
      { status: 500 },
    );
  }
}

export async function POST(request: Request): Promise<NextResponse> {
  try {
    const body = (await request.json()) as HitlPayload;
    if (!body.action || !body.task_id) {
      return NextResponse.json(
        { error: "The action and task_id fields are required" },
        { status: 400 },
      );
    }
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(body.task_id)) {
      return NextResponse.json({ error: "Invalid task_id" }, { status: 400 });
    }

    let endpoint: string = body.action;
    let payload: Record<string, string> | undefined;
    if (body.action === "inject-hint") {
      if (!body.hint_text?.trim()) {
        return NextResponse.json(
          { error: "inject-hint requires hint_text" },
          { status: 400 },
        );
      }
      endpoint = "directive";
      payload = { body: body.hint_text.trim() };
    } else if (body.action === "abort") {
      payload = { reason: body.reason ?? "operator_request" };
    } else if (body.action === "boost" || body.action === "retract") {
      if (!body.entry_id || !/^[a-zA-Z0-9_-]{1,64}$/.test(body.entry_id)) {
        return NextResponse.json(
          { error: `${body.action} requires entry_id` },
          { status: 400 },
        );
      }
      endpoint = "steer";
      payload = { action: body.action, entry_id: body.entry_id };
    } else if (body.action === "approval") {
      if (!body.run_id || !body.choice || !APPROVAL_CHOICES.has(body.choice)) {
        return NextResponse.json(
          { error: "approval requires run_id and a valid choice" },
          { status: 400 },
        );
      }
      payload = {
        run_id: body.run_id,
        choice: body.choice,
        reason: body.reason ?? "",
      };
    } else if (body.action === "run-steer") {
      const input = body.input?.trim();
      if (!body.run_id || !input) {
        return NextResponse.json(
          { error: "run-steer requires run_id and input" },
          { status: 400 },
        );
      }
      if (input.length > 2_000) {
        return NextResponse.json(
          { error: "run-steer input must contain at most 2000 characters" },
          { status: 400 },
        );
      }
      endpoint = "run-steer";
      payload = { run_id: body.run_id, input };
    }

    const upstream = await fetch(
      `${DAEMON_BASE_URL}/api/tasks/${encodeURIComponent(body.task_id)}/${endpoint}`,
      {
        method: "POST",
        headers: {
          ...daemonHeaders(),
          ...(request.headers.get("X-Idempotency-Key")
            ? { "X-Idempotency-Key": request.headers.get("X-Idempotency-Key") as string }
            : {}),
          ...(request.headers.get("X-BMAS-Operator-Id")
            ? { "X-Operator-Id": request.headers.get("X-BMAS-Operator-Id") as string }
            : {}),
        },
        ...(payload ? { body: JSON.stringify(payload) } : {}),
        signal: AbortSignal.timeout(15_000),
      },
    );
    const data = await upstream.json().catch(() => ({}));
    return NextResponse.json(data, { status: upstream.status });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Unknown daemon error";
    return NextResponse.json(
      { error: "Task control request failed", detail: message },
      { status: 503 },
    );
  }
}
