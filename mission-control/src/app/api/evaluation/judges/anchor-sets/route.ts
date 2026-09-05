import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(request: Request) {
  const now = new URL(request.url).searchParams.get("now");
  return evaluationProxy(now ? `/judges/anchor-sets?now=${encodeURIComponent(now)}` : "/judges/anchor-sets");
}

export async function POST(request: Request) {
  return evaluationProxy("/judges/anchor-sets", { request, method: "POST" });
}
