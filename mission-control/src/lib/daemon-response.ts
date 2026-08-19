import { NextResponse } from "next/server";

export async function daemonJsonResponse(response: Response): Promise<NextResponse> {
  const raw = await response.text();
  let data: unknown;
  try {
    data = raw ? JSON.parse(raw) as unknown : {};
  } catch {
    data = {
      error: response.ok ? "The daemon returned invalid JSON" : "Daemon request failed",
      detail: raw.slice(0, 1_000),
    };
  }
  return NextResponse.json(data, { status: response.status });
}

export function daemonFailure(error: unknown, message: string): NextResponse {
  return NextResponse.json(
    {
      error: message,
      detail: error instanceof Error ? error.message : "Unknown daemon error",
    },
    { status: 503 },
  );
}

export function daemonMutationHeaders(): Record<string, string> {
  return process.env.BMAS_API_KEY
    ? { Authorization: `Bearer ${process.env.BMAS_API_KEY}` }
    : {};
}
