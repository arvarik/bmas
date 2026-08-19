import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ testId: string }> },
) {
  const { testId } = await params;
  return benchmarkProxy(`/benchmarks/tests/${encodeURIComponent(testId)}`);
}
