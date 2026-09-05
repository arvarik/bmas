import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ attemptId: string }> },
) {
  const { attemptId } = await params;
  return evaluationProxy(`/attempts/${encodeURIComponent(attemptId)}/score-records`);
}
