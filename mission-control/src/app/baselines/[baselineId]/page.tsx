import { BaselineDetailClient } from "./BaselineDetailClient";

export default async function BaselineDetailPage({ params }: { params: Promise<{ baselineId: string }> }) {
  const { baselineId } = await params;
  return <BaselineDetailClient baselineId={baselineId} />;
}
