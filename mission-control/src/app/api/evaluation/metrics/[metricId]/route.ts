import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ metricId: string }> },
) {
  const { metricId } = await params;
  return evaluationProxy(`/metrics/${encodeURIComponent(metricId)}`);
}
