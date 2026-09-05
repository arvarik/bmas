import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ datasetId: string; versionId: string }> },
) {
  const { datasetId, versionId } = await params;
  return evaluationProxy(
    `/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(versionId)}/record`,
  );
}
