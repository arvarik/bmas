import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET() {
  return evaluationProxy("/studies");
}

export async function POST(request: Request) {
  return evaluationProxy("/studies", { request, method: "POST" });
}
