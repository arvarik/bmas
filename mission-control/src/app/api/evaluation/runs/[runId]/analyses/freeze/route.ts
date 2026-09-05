import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  return evaluationProxy(
    `/runs/${encodeURIComponent(runId)}/analyses/freeze`,
    { request, method: "POST" },
  );
}
