import { evaluationProxy } from "@/lib/evaluation-proxy";

export async function POST(
  request: Request,
  { params }: { params: Promise<{ anchorId: string }> },
) {
  const { anchorId } = await params;
  const calibratedAt = new URL(request.url).searchParams.get("calibrated_at");
  const path = `/judges/anchor-sets/${encodeURIComponent(anchorId)}/calibrate`;
  return evaluationProxy(
    calibratedAt ? `${path}?calibrated_at=${encodeURIComponent(calibratedAt)}` : path,
    { request, method: "POST" },
  );
}
