import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  request: Request,
  { params }: { params: Promise<{ runId: string }> },
) {
  const { runId } = await params;
  const currency = new URL(request.url).searchParams.get("currency") ?? "USD";
  return evaluationProxy(
    `/runs/${encodeURIComponent(runId)}/resource-ledger?currency=${encodeURIComponent(currency)}`,
  );
}
