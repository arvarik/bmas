import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ testId: string; revisionId: string }> },
) {
  const { testId, revisionId } = await params;
  return benchmarkProxy(
    `/benchmarks/tests/${encodeURIComponent(testId)}/revisions/${encodeURIComponent(revisionId)}/runs`,
    { request, method: "POST" },
  );
}
