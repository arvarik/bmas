import { DAEMON_BASE_URL } from "@/lib/config";
import { daemonFailure } from "@/lib/daemon-response";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ datasetId: string; versionId: string }> },
) {
  const { datasetId, versionId } = await params;
  try {
    const response = await fetch(
      `${DAEMON_BASE_URL}/datasets/${encodeURIComponent(datasetId)}/versions/${encodeURIComponent(versionId)}/source`,
      { cache: "no-store", signal: AbortSignal.timeout(30_000) },
    );
    if (!response.ok) {
      const detail = await response.text().catch(() => "Dataset source request failed");
      return Response.json({ error: detail }, { status: response.status });
    }
    return new Response(response.body, {
      status: response.status,
      headers: {
        "Content-Type": response.headers.get("Content-Type") || "application/octet-stream",
        "Content-Disposition": response.headers.get("Content-Disposition") || "attachment",
        "Cache-Control": "private, no-store",
      },
    });
  } catch (error) {
    return daemonFailure(error, "Dataset source is unavailable");
  }
}
