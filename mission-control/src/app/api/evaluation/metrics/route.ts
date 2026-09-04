import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(request: Request) {
  const incoming = new URL(request.url).searchParams;
  const params = new URLSearchParams();
  const state = incoming.get("lifecycle_state");
  if (state) params.set("lifecycle_state", state);
  const query = params.toString();
  return evaluationProxy(`/metrics${query ? `?${query}` : ""}`);
}

export async function POST(request: Request) {
  return evaluationProxy("/metrics", { request, method: "POST" });
}
