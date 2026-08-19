import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  return benchmarkProxy(`/benchmarks/runs/${encodeURIComponent(runId)}`);
}
