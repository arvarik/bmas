import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ runtimeId: string }> },
) {
  const { runtimeId } = await params;
  return benchmarkProxy(`/benchmarks/runtimes/${encodeURIComponent(runtimeId)}/qualify`, {
    request,
    method: "POST",
  });
}
