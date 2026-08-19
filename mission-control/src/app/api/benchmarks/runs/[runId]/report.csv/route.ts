import { benchmarkRawProxy } from "@/lib/benchmark-proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  return benchmarkRawProxy(
    `/benchmarks/runs/${encodeURIComponent(runId)}/report.csv${new URL(request.url).search}`,
  );
}
