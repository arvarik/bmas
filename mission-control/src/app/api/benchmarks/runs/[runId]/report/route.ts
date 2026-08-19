import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  return benchmarkProxy(
    `/benchmarks/runs/${encodeURIComponent(runId)}/report${new URL(request.url).search}`,
  );
}
