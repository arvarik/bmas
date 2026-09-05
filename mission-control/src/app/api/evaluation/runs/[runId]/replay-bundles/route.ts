import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  const incoming = new URL(request.url).searchParams;
  const query = new URLSearchParams();
  query.set("policy", incoming.get("policy") ?? "redacted");
  const snapshotId = incoming.get("snapshot_id");
  if (snapshotId) query.set("snapshot_id", snapshotId);
  return evaluationProxy(
    `/runs/${encodeURIComponent(runId)}/replay-bundles?${query.toString()}`,
    { request, method: "POST" },
  );
}
