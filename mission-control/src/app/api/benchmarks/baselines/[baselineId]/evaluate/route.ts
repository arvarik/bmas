import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ baselineId: string }> },
) {
  const { baselineId } = await params;
  return benchmarkProxy(
    `/benchmarks/baselines/${encodeURIComponent(baselineId)}/evaluate`,
    { request, method: "POST" },
  );
}
