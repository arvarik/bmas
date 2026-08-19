import { benchmarkProxy } from "@/lib/benchmark-proxy";

export async function GET() {
  return benchmarkProxy("/benchmarks/scorers");
}
