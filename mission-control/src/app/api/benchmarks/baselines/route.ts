import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function GET(request: Request) {
  return benchmarkProxy(`/benchmarks/baselines${new URL(request.url).search}`);
}

export async function POST(request: Request) {
  return benchmarkProxy("/benchmarks/baselines", { request, method: "POST" });
}
