"use client";

/**
 * Artifacts tab — shows agent-created output files for a task.
 */

import { useParams } from "next/navigation";
import { ArtifactBrowser } from "@/components/features/ArtifactBrowser";

export default function ArtifactsPage() {
  const { taskId } = useParams();
  return <ArtifactBrowser taskId={taskId as string} />;
}
