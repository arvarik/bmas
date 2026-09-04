import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ runId: string; snapshotId: string }> },
) {
  const { runId, snapshotId } = await params;
  return evaluationProxy(
    `/runs/${encodeURIComponent(runId)}/analyses/${encodeURIComponent(snapshotId)}/overview`,
  );
}
