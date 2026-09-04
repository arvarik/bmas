import { MetricDetailClient } from "./MetricDetailClient";

export default async function MetricDetailPage({ params }: { params: Promise<{ metricId: string }> }) {
  const { metricId } = await params;
  return <MetricDetailClient metricId={metricId} />;
}
