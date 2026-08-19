import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function GET(request: Request) {
  return benchmarkProxy(`/benchmarks/runs${new URL(request.url).search}`);
}
