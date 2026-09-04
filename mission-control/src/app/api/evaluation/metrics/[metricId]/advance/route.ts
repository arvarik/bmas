import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ metricId: string }> },
) {
  const { metricId } = await params;
  return evaluationProxy(
    `/metrics/${encodeURIComponent(metricId)}/advance`,
    { request, method: "POST" },
  );
}
