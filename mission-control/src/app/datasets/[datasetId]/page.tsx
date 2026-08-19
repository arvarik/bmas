import { DatasetDetailClient } from "./DatasetDetailClient";

export default async function DatasetDetailPage({
  params,
}: {
  params: Promise<{ datasetId: string }>;
}) {
  const { datasetId } = await params;
  return <DatasetDetailClient datasetId={datasetId} />;
}
