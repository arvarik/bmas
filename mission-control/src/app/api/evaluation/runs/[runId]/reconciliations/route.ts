import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  return evaluationProxy(`/runs/${encodeURIComponent(runId)}/reconciliations`);
}

export async function POST(
  request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  return evaluationProxy(
    `/runs/${encodeURIComponent(runId)}/reconciliations`,
    { request, method: "POST" },
  );
}
