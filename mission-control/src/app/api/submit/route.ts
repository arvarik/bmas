import { NextResponse } from "next/server";
import { DAEMON_BASE_URL, DAEMON_SUBMIT_URL } from "@/lib/config";

interface TaskRoutingOverride {
  simple?: string;
  light?: string;
  medium?: string;
  complex?: string;
}

interface TaskRoleEntryOverride {
  preferred_host?: string | null;
  profile?: string;
  dispatch_port?: number;
}

interface TaskOverrides {
  routing?: TaskRoutingOverride;
  role_registry?: Record<string, TaskRoleEntryOverride>;
  classic?: Record<string, unknown>;
}

interface SubmitPayload {
  task: string;
  variant?: string;
  effort?: string;
  overrides?: TaskOverrides;
}

export async function POST(request: Request): Promise<NextResponse> {
  try {
    const contentType = request.headers.get("content-type") ?? "";
    if (contentType.startsWith("multipart/form-data")) {
      if (!request.body) {
        return NextResponse.json(
          { error: "Missing multipart request body" },
          { status: 400 },
        );
      }
      const init: RequestInit & { duplex: "half" } = {
        method: "POST",
        headers: {
          "Content-Type": contentType,
          ...(process.env.BMAS_API_KEY
            ? { Authorization: `Bearer ${process.env.BMAS_API_KEY}` }
            : {}),
        },
        body: request.body,
        duplex: "half",
        signal: AbortSignal.timeout(120_000),
      };
      const upstream = await fetch(
        `${DAEMON_BASE_URL}/submit-with-files`,
        init,
      );
      return daemonResponse(upstream);
    }

    const body = (await request.json()) as SubmitPayload;

    if (!body.task || typeof body.task !== "string" || !body.task.trim()) {
      return NextResponse.json(
        { error: "Missing or empty 'task' field" },
        { status: 400 },
      );
    }

    const upstream = await fetch(DAEMON_SUBMIT_URL, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(process.env.BMAS_API_KEY
          ? { Authorization: `Bearer ${process.env.BMAS_API_KEY}` }
          : {}),
      },
      body: JSON.stringify({
        task: body.task.trim(),
        ...(body.variant ? { variant: body.variant } : {}),
        ...(typeof body.effort === "string" && body.effort ? { effort: body.effort } : {}),
        ...(body.overrides ? { overrides: body.overrides } : {}),
      }),
      signal: AbortSignal.timeout(5_000), // daemon responds immediately (HTTP 202)
    });

    return daemonResponse(upstream);
  } catch (err) {
    const message =
      err instanceof Error ? err.message : "Unknown upstream error";
    return NextResponse.json(
      { error: "Task submission failed", detail: message },
      { status: 503 },
    );
  }
}

async function daemonResponse(upstream: Response): Promise<NextResponse> {
  const text = await upstream.text().catch(() => "");
  let data: unknown;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    data = upstream.ok
      ? { status: "ok" }
      : { error: `Daemon returned ${upstream.status}`, detail: text };
  }
  return NextResponse.json(data, { status: upstream.status });
}
