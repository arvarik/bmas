import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ digest: string }> },
) {
  const { digest } = await params;
  return evaluationProxy(`/evidence/sections/${encodeURIComponent(digest)}`);
}
