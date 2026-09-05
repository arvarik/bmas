import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function POST(request: Request) {
  return evaluationProxy("/replay-bundles/import", { request, method: "POST" });
}
