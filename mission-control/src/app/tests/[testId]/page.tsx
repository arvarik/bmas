import { TestDetailClient } from "./TestDetailClient";

export default async function TestDetailPage({ params }: { params: Promise<{ testId: string }> }) {
  const { testId } = await params;
  return <TestDetailClient testId={testId} />;
}
