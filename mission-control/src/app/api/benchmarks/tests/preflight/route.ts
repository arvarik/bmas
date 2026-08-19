import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function POST(request: Request) {
  return benchmarkProxy("/benchmarks/tests/preflight", { request, method: "POST" });
}
