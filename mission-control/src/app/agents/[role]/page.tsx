import { AgentDetailClient } from "./AgentDetailClient";

export default async function AgentDetailPage({
  params,
}: {
  params: Promise<{ role: string }>;
}) {
  const { role } = await params;
  return <AgentDetailClient role={decodeURIComponent(role)} />;
}
