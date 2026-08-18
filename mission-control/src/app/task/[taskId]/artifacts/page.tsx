"use client";

/**
 * Artifacts tab — shows agent-created output files for a task.
 */

import { useParams } from "next/navigation";
import { ArtifactBrowser } from "@/components/features/ArtifactBrowser";
import { useTaskData } from "../TaskStreamContext";

export default function ArtifactsPage() {
  const { taskId } = useParams();
  const { liveArtifacts } = useTaskData();
  return (
    <ArtifactBrowser
      taskId={taskId as string}
      liveArtifacts={liveArtifacts}
    />
  );
}
