import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function GET(request: Request) {
  const query = new URL(request.url).search;
  return benchmarkProxy(`/benchmarks/tests${query}`);
}

export async function POST(request: Request) {
  return benchmarkProxy("/benchmarks/tests", { request, method: "POST" });
}
