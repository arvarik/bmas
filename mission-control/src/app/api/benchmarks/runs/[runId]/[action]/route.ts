import { NextResponse } from "next/server";
import { benchmarkProxy } from "@/lib/benchmark-proxy";

const ACTIONS = new Set(["pause", "resume", "cancel", "retry"]);

export async function POST(
  request: Request,
  { params }: { params: Promise<{ runId: string; action: string }> },
) {
  const { runId, action } = await params;
  if (!ACTIONS.has(action)) {
    return NextResponse.json({ error: "The run action is invalid" }, { status: 404 });
  }
  return benchmarkProxy(
    `/benchmarks/runs/${encodeURIComponent(runId)}/${action}`,
    { request, method: "POST" },
  );
}
