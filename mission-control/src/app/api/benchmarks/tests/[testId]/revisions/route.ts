import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ testId: string }> },
) {
  const { testId } = await params;
  return benchmarkProxy(`/benchmarks/tests/${encodeURIComponent(testId)}/revisions`, {
    request,
    method: "POST",
  });
}
