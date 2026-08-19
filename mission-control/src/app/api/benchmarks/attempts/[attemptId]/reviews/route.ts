import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ attemptId: string }> },
) {
  const { attemptId } = await params;
  return benchmarkProxy(
    `/benchmarks/attempts/${encodeURIComponent(attemptId)}/reviews`,
    { request, method: "POST" },
  );
}
