import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ judgeId: string; version: string }> },
) {
  const { judgeId, version } = await params;
  return evaluationProxy(
    `/judges/${encodeURIComponent(judgeId)}/versions/${encodeURIComponent(version)}/calibration`,
  );
}
